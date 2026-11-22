import asyncio
import json
import os
import random
import re
import secrets
import string
import threading
import time
from typing import Optional, Tuple

import aiohttp
from flask import Flask, jsonify
from vkbottle import Bot, Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Message
from vkbottle.tools import DocUploader

# =====================================================================
# 1. КОНФИГУРАЦИЯ И ИНИЦИАЛИЗАЦИЯ
# =====================================================================
TOKEN = os.getenv(
    "BOT_TOKEN",
    "vk1.a.5BfnzsvcbvRGCC9ZGxmbKdzOU-0NBWIPc9RovnKuIImAHcV46i4wMJUGvrkua3xKhDqo57KVoiXQOqhuctwA93--pk54OWAy_rfzgGfTTQFdG1vFzRObXKPTikSt3h1rRR6ImqGbPMaOgAC3vLaPxx359f9T4N2nl2d4dZGFInNv4F9LLsctf3GiuJfrBkqBcji79CiXrPLqeClPoXaptg",
).strip()
MAIN_OWNER_ID = int(os.getenv("OWNER_ID", "897082738"))
PORT = int(os.getenv("PORT", "10000"))
DB_FILE = os.getenv("DB_FILE", "bot_database.json")
START_TIME = time.time()

DEFAULT_PROJECT_NAME = "GAMES | Чат Менеджер"
DEFAULT_PROJECT_URL = "https://vk.com/GamesChatMeneger"
DEFAULT_RULES = (
    "📜 ═══ ПРАВИЛА БЕСЕДЫ ═══ 📜\n\n"
    "1. Запрещён спам, флуд и реклама сторонних ресурсов.\n"
    "2. Уважайте участников и администрацию чата.\n"
    "3. Запрещён чрезмерный капс и токсичное поведение.\n"
    "4. Решения модерации обсуждаются корректно."
)

DEFAULT_ROLES = {
    0: "Участник",
    15: "Младший состав",
    45: "Модератор",
    55: "Старший модератор",
    60: "Руководитель",
    70: "Старший администратор",
    90: "Высший состав",
    95: "Куратор сети",
    100: "Владелец чата",
}

MAX_ACTIVE_POOL_CHATS = 1000
BONUS_MIN = 1_000_000
BONUS_MAX = 50_000_000

bot = Bot(token=TOKEN)
doc_uploader = DocUploader(bot.api)

# =====================================================================
# FLASK HEALTH SERVER ДЛЯ RENDER / UPTIMEROBOT
# =====================================================================
flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health_endpoint():
    return jsonify({"status": "ok", "uptime": int(time.time() - START_TIME)}), 200


def start_flask_server() -> None:
    threading.Thread(
        target=lambda: flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False),
        daemon=True,
    ).start()


# =====================================================================
# 2. БАЗА ДАННЫХ И СОСТОЯНИЯ
# =====================================================================
DB_LOCK = threading.RLock()


def default_db() -> dict:
    return {
        "chats": {},
        "global_chats": [],
        "global_bans": {},
        "global_roles": {},
        "global_reprimands": {},
        "leaders": {},
        "reports": [],
        "logs": [],
        "chat_messages_log": [],
        "bot_interactions_log": [],
        "broadcast_admins": [],
        "pool_codes": {},
        "economy": {},
        "pending_bans": {},
        "pending_duels": {},
    }


def load_db() -> dict:
    base = default_db()
    if not os.path.exists(DB_FILE):
        return base
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return base
        for k, v in base.items():
            if k not in data or type(data[k]) is not type(v):
                data[k] = v
        return data
    except Exception:
        return base


def save_db(data: dict) -> None:
    with DB_LOCK:
        tmp_file = f"{DB_FILE}.tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_file, DB_FILE)
        except Exception as e:
            print(f"[DB SAVE ERROR] {e}")


db = load_db()
save_db(db)

user_states: dict[int, str] = {}
chat_setup_states: dict[tuple[int, int], dict] = {}
user_cache: dict[int, dict] = {}
flood_tracker: dict[tuple[int, int], list[float]] = {}
rep_cooldown: dict[tuple[int, int], float] = {}


def is_bot_owner(uid: int) -> bool:
    return int(uid) == int(MAIN_OWNER_ID)


def add_log(entry: str) -> None:
    with DB_LOCK:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        db["logs"].append(f"[{stamp}] {entry}")
        db["logs"] = db["logs"][-200:]
        save_db(db)


def record_chat_message(peer_id: int, uid: int, text: str) -> None:
    with DB_LOCK:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        db["chat_messages_log"].append({
            "ts": stamp,
            "peer_id": int(peer_id),
            "uid": int(uid),
            "text": text[:300],
        })
        db["chat_messages_log"] = db["chat_messages_log"][-500:]
        save_db(db)


def record_bot_interaction(peer_id: int, uid: int, text: str) -> None:
    with DB_LOCK:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        db["bot_interactions_log"].append({
            "ts": stamp,
            "peer_id": int(peer_id),
            "uid": int(uid),
            "text": text[:300],
        })
        db["bot_interactions_log"] = db["bot_interactions_log"][-500:]
        save_db(db)


def get_chat_id_from_peer(message: Message) -> int | None:
    cid = getattr(message, "chat_id", None)
    if isinstance(cid, int) and cid > 0:
        return cid
    if message.peer_id > 2000000000:
        return message.peer_id - 2000000000
    return None


def get_chat_data(peer_id: int) -> dict:
    with DB_LOCK:
        pid = str(peer_id)
        default_chat = {
            "owner_id": None,
            "welcomed": False,
            "locked": False,
            "antispam": False,
            "caps_guard": False,
            "antiflood": False,
            "antiads": False,
            "project_name": DEFAULT_PROJECT_NAME,
            "project_url": DEFAULT_PROJECT_URL,
            "rules_text": DEFAULT_RULES,
            "greeting_text": "",
            "members": {},
            "warns": {},
            "reprimands": {},
            "mutes": {},
            "bans": {},
            "notes": {},
            "reputation": {},
            "role_names": {str(k): v for k, v in DEFAULT_ROLES.items()},
            "pools": {
                "global_cmds": False,
                "global_code": None,
                "nick_roles": False,
                "nick_roles_code": None,
            },
        }
        if pid not in db["chats"] or not isinstance(db["chats"][pid], dict):
            db["chats"][pid] = default_chat
            return db["chats"][pid]

        chat = db["chats"][pid]
        for k, v in default_chat.items():
            if k not in chat:
                chat[k] = v
        if not isinstance(chat.get("pools"), dict):
            chat["pools"] = default_chat["pools"].copy()
        else:
            for pk, pv in default_chat["pools"].items():
                if pk not in chat["pools"]:
                    chat["pools"][pk] = pv
        if not isinstance(chat.get("role_names"), dict) or not chat["role_names"]:
            chat["role_names"] = {str(k): v for k, v in DEFAULT_ROLES.items()}
        return chat


# =====================================================================
# ЭКОНОМИКА: МОНЕТЫ, БОНУС 1-50КК, VIP (1 ДЕНЬ - 10 ЛЕТ)
# =====================================================================
def get_econ(uid: int) -> dict:
    with DB_LOCK:
        key = str(uid)
        if key not in db["economy"] or not isinstance(db["economy"][key], dict):
            db["economy"][key] = {
                "coins": 0,
                "vip_until": 0,
                "last_bonus": 0,
                "wins": 0,
                "games": 0,
            }
            save_db(db)
        rec = db["economy"][key]
        rec.setdefault("coins", 0)
        rec.setdefault("vip_until", 0)
        rec.setdefault("last_bonus", 0)
        rec.setdefault("wins", 0)
        rec.setdefault("games", 0)
        return rec


def is_vip_active(uid: int) -> Tuple[bool, int]:
    rec = get_econ(uid)
    left = int(rec.get("vip_until", 0)) - int(time.time())
    return (left > 0, max(0, left))


def format_coins(amount: int) -> str:
    return f"{int(amount):,}".replace(",", " ")


def format_duration_ru(seconds: int) -> str:
    if seconds <= 0:
        return "0 сек."
    days = seconds // 86400
    rem = seconds % 86400
    hours = rem // 3600
    mins = (rem % 3600) // 60
    years = days // 365
    if years >= 1:
        return f"{years} г. ({days} дн.)"
    if days > 0:
        return f"{days} дн. {hours} ч."
    if hours > 0:
        return f"{hours} ч. {mins} мин."
    return f"{max(1, mins)} мин."


def parse_vip_seconds(raw: str) -> Optional[int]:
    s = raw.strip().lower().replace(",", ".")
    max_days = 10 * 365
    if s.isdigit():
        days = int(s)
    else:
        m = re.fullmatch(
            r"(\d+)\s*(d|д|дн|день|дня|дней|w|н|нед|неделя|недель|m|мес|месяц|месяца|месяцев|y|г|год|года|лет)",
            s,
        )
        if not m:
            return None
        val = int(m.group(1))
        unit = m.group(2)
        if unit in ("d", "д", "дн", "день", "дня", "дней"):
            days = val
        elif unit in ("w", "н", "нед", "неделя", "недель"):
            days = val * 7
        elif unit in ("m", "мес", "месяц", "месяца", "месяцев"):
            days = val * 30
        elif unit in ("y", "г", "год", "года", "лет"):
            days = val * 365
        else:
            return None
    if days < 1 or days > max_days:
        return None
    return days * 86400


def record_game_result(uid: int, delta_coins: int, won: bool) -> int:
    with DB_LOCK:
        econ = get_econ(uid)
        econ["coins"] = max(0, int(econ.get("coins", 0)) + int(delta_coins))
        econ["games"] = int(econ.get("games", 0)) + 1
        if won:
            econ["wins"] = int(econ.get("wins", 0)) + 1
        save_db(db)
        return int(econ["coins"])


# =====================================================================
# 3. СИСТЕМА РАНГОВ, ПУЛОВ И БЕСКОНЕЧНЫХ КОДОВ (1 КОД = ∞ ЧАТОВ)
# =====================================================================
def count_active_pool_chats() -> int:
    active = 0
    for c_data in db.get("chats", {}).values():
        if not isinstance(c_data, dict):
            continue
        pools = c_data.get("pools", {})
        if pools.get("global_cmds") or pools.get("nick_roles"):
            active += 1
    return active


def has_global_pool(chat_data: dict) -> bool:
    return bool(chat_data.get("pools", {}).get("global_cmds"))


def has_nick_roles_pool(chat_data: dict) -> bool:
    return bool(chat_data.get("pools", {}).get("nick_roles"))


def generate_random_code(pool_type: str, created_by: int) -> str:
    with DB_LOCK:
        alphabet = string.ascii_uppercase + string.digits
        while True:
            part1 = "".join(secrets.choice(alphabet) for _ in range(4))
            part2 = "".join(secrets.choice(alphabet) for _ in range(4))
            code = f"POOL-{part1}-{part2}"
            if code not in db["pool_codes"]:
                break
        db["pool_codes"][code] = {
            "type": pool_type,
            "created_by": created_by,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chats": [],
            "infinite": True,
        }
        save_db(db)
        return code


def activate_pool_code_for_chat(peer_id: int, raw_code: str) -> tuple[bool, str]:
    with DB_LOCK:
        code = raw_code.strip().upper()
        code_obj = db.get("pool_codes", {}).get(code)
        if not code_obj:
            return False, f"❌ Код `{code}` не найден или был отозван."

        chats_list = [int(x) for x in code_obj.get("chats", [])]
        chat_data = get_chat_data(peer_id)

        was_active_in_pool = has_global_pool(chat_data) or has_nick_roles_pool(chat_data)
        if not was_active_in_pool and count_active_pool_chats() >= MAX_ACTIVE_POOL_CHATS:
            return False, f"⚠️ Достигнут лимит активных чатов в пуле (`{MAX_ACTIVE_POOL_CHATS}/{MAX_ACTIVE_POOL_CHATS}`)."

        p_type = code_obj.get("type", "all")
        enabled_parts = []

        if p_type in ("global", "all"):
            chat_data["pools"]["global_cmds"] = True
            chat_data["pools"]["global_code"] = code
            enabled_parts.append("пул сетевых команд `/г...` и `/g...`")

        if p_type in ("nick_roles", "all"):
            chat_data["pools"]["nick_roles"] = True
            chat_data["pools"]["nick_roles_code"] = code
            enabled_parts.append("отдельный пул «Ники и роли»")

        if peer_id not in chats_list:
            chats_list.append(peer_id)
            code_obj["chats"] = chats_list

        if peer_id not in db["global_chats"]:
            db["global_chats"].append(peer_id)

        save_db(db)
        add_log(f"Активирован бесконечный код {code} ({p_type}) в чате {peer_id}. Всего в коде: {len(chats_list)} чатов")

        joined = " и ".join(enabled_parts) if enabled_parts else "доступ"
        return True, (
            f"✅ **Бесконечный код `{code}` активирован (`∞` чатов)!**\n"
            f"• Подключено: **{joined}**\n"
            f"• Чатов активировано по этому коду: `{len(chats_list)}`\n"
            f"• Всего активных чатов в пуле: `{count_active_pool_chats()}`"
        )


def revoke_pool_code(raw_code: str) -> tuple[bool, str]:
    with DB_LOCK:
        code = raw_code.strip().upper()
        code_obj = db.get("pool_codes", {}).pop(code, None)
        if not code_obj:
            return False, f"❌ Код `{code}` не найден."

        for cid in code_obj.get("chats", []):
            c_data = get_chat_data(int(cid))
            if c_data["pools"].get("global_code") == code:
                c_data["pools"]["global_cmds"] = False
                c_data["pools"]["global_code"] = None
            if c_data["pools"].get("nick_roles_code") == code:
                c_data["pools"]["nick_roles"] = False
                c_data["pools"]["nick_roles_code"] = None
        save_db(db)
        add_log(f"Отозван и удалён код {code}")
        return True, f"🗑 Код `{code}` отозван и удалён."


def is_chat_owner(uid: int, chat_data: dict, owner_id: int | None) -> bool:
    if is_bot_owner(uid):
        return True
    if owner_id is not None and uid == int(owner_id):
        return True
    if chat_data.get("owner_id") is not None and uid == int(chat_data["owner_id"]):
        return True
    return False


def sync_owner_member(chat_data: dict, owner_uid: int) -> None:
    chat_data["owner_id"] = int(owner_uid)
    o_str = str(owner_uid)
    cur = chat_data["members"].get(o_str, {})
    nick = cur.get("nickname", "") if isinstance(cur, dict) else ""
    chat_data["members"][o_str] = {"level": 100, "nickname": nick}


def get_role_title(level: int, chat_data: dict, owner_flag: bool = False) -> str:
    if owner_flag or level >= 100:
        return chat_data.get("role_names", {}).get("100") or DEFAULT_ROLES[100]
    role_names = chat_data.get("role_names", {})
    exact = role_names.get(str(level))
    if exact:
        return exact
    numeric_levels = sorted((int(k) for k in role_names.keys() if str(k).isdigit()), reverse=True)
    for lvl in numeric_levels:
        if level >= lvl:
            return role_names[str(lvl)]
    return role_names.get("0") or DEFAULT_ROLES[0]


def get_user_level_by_id(uid: int, chat_data: dict, owner_id: int | None = None) -> int:
    if is_bot_owner(uid):
        return 100
    if is_chat_owner(uid, chat_data, owner_id):
        return 100
    u_str = str(uid)
    local_lvl = 0
    m_info = chat_data.get("members", {}).get(u_str)
    if isinstance(m_info, dict):
        local_lvl = int(m_info.get("level", 0) or 0)
    elif isinstance(m_info, (int, float, str)) and str(m_info).isdigit():
        local_lvl = int(m_info)

    global_lvl = int(db.get("global_roles", {}).get(u_str, 0) or 0) if has_global_pool(chat_data) else 0
    return max(local_lvl, global_lvl)


# =====================================================================
# 4. БЭКАП / СКАЧИВАНИЕ / ЗАГРУЗКА БД И МОНИТОРИНГ ЧАТОВ (ВЛАДЕЛЕЦ)
# =====================================================================
async def send_db_file(target_peer_id: int, caption: str = "💾 База данных bot_database.json") -> Tuple[bool, str]:
    save_db(db)
    if not os.path.exists(DB_FILE):
        return False, "Файл БД не найден."
    try:
        attachment = await doc_uploader.upload(
            file_source=DB_FILE,
            peer_id=int(target_peer_id),
            title="bot_database.json",
        )
        await bot.api.messages.send(
            peer_id=int(target_peer_id),
            message=caption,
            attachment=attachment,
            random_id=random.randint(1, 2_000_000_000),
        )
        return True, "ok"
    except Exception as e:
        err_msg = f"Ошибка отправки файла ВК: {e}"
        print(f"[DB SEND ERROR] {err_msg}")
        return False, err_msg


async def restore_db_from_message(message: Message) -> Tuple[bool, str]:
    doc_url = None
    if message.attachments:
        for att in message.attachments:
            if getattr(att, "doc", None) and getattr(att.doc, "url", None):
                doc_url = att.doc.url
                break
    if not doc_url and message.reply_message and message.reply_message.attachments:
        for att in message.reply_message.attachments:
            if getattr(att, "doc", None) and getattr(att.doc, "url", None):
                doc_url = att.doc.url
                break

    if not doc_url:
        return False, "📎 Прикрепи файл `.json` к сообщению с `/загрузитьбд` или ответь командой на сообщение с документом."

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(doc_url) as resp:
                if resp.status != 200:
                    return False, f"❌ Не удалось скачать файл (HTTP {resp.status})."
                raw_bytes = await resp.read()
        parsed = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(parsed, dict):
            return False, "❌ Неверный формат JSON."
    except Exception as e:
        return False, f"❌ Ошибка при чтении JSON: `{e}`"

    with DB_LOCK:
        base = default_db()
        db.clear()
        for k, v in base.items():
            if k in parsed and type(parsed[k]) is type(v):
                db[k] = parsed[k]
            else:
                db[k] = v
        save_db(db)

    add_log("Владелец бота загрузил и восстановил базу данных.")
    return True, "✅ База данных успешно загружена и сохранена на диск!"


# =====================================================================
# 5. ОПРЕДЕЛЕНИЕ ВЛАДЕЛЬЦА БЕСЕДЫ, РАССЫЛКА И ПРИВЕТСТВИЕ
# =====================================================================
async def resolve_chat_owner(peer_id: int, chat_data: dict) -> tuple[str, int | None]:
    chat_title = f"Беседа #{peer_id - 2000000000}" if peer_id > 2000000000 else f"Чат {peer_id}"
    owner_id = chat_data.get("owner_id")

    try:
        conv_res = await bot.api.messages.get_conversations_by_id(peer_ids=[peer_id])
        if conv_res.items:
            conv = conv_res.items[0]
            if conv.chat_settings and conv.chat_settings.title:
                chat_title = conv.chat_settings.title
            if conv.chat_settings and getattr(conv.chat_settings, "owner_id", None):
                vk_owner = int(conv.chat_settings.owner_id)
                if vk_owner > 0:
                    owner_id = vk_owner
                    sync_owner_member(chat_data, vk_owner)
    except Exception:
        pass

    if not owner_id:
        try:
            members_res = await bot.api.messages.get_conversation_members(peer_id=peer_id)
            for item in (members_res.items or []):
                if getattr(item, "is_owner", False) and int(item.member_id) > 0:
                    owner_id = int(item.member_id)
                    sync_owner_member(chat_data, owner_id)
                    break
        except Exception:
            pass

    return chat_title, int(owner_id) if owner_id else None


async def get_vk_chat_member_flags(peer_id: int, uid: int) -> tuple[bool, bool]:
    try:
        members_res = await bot.api.messages.get_conversation_members(peer_id=peer_id)
        for item in (members_res.items or []):
            if int(item.member_id) == int(uid):
                return bool(getattr(item, "is_owner", False)), bool(getattr(item, "is_admin", False))
    except Exception:
        pass
    return False, False


def build_welcome_guide(chat_data: dict) -> str:
    proj_name = chat_data.get("project_name", DEFAULT_PROJECT_NAME)
    return (
        f"👋 ═══ {proj_name} — БОТ ПОДКЛЮЧЁН ═══ 👋\n\n"
        "• Справка: `/хелп` или кнопка `📖 Команды` | Меню: `/меню`\n"
        "• Настройка чата кнопками: `⚙️ Настройка чата` (приветствие, правила, название, ссылка, защиты)\n"
        "• Выдача `100 lvl` владельцу беседы: кнопка `👑 Забрать 100 lvl` или `/явладелец`\n"
        "• Бонус `1 000 000 – 50 000 000`: `/бонус` | Бан кнопками (`1–360` дней): `/бан @юзер`"
    )


async def run_global_broadcast(sender_id: int, raw_body: str) -> int:
    count = 0
    sender_fmt = await format_vk_user(sender_id)
    broadcast_text = (
        "📢 **ВАЖНОЕ ОБЪЯВЛЕНИЕ** 📢\n\n"
        f"{raw_body}\n\n"
        "═══════════════════════════\n"
        f"👤 Отправитель: {sender_fmt}\n"
        "@all"
    )
    targets = set(int(x) for x in db.get("global_chats", [])) | set(int(x) for x in db.get("chats", {}).keys())
    for cid in targets:
        try:
            await bot.api.messages.send(peer_id=int(cid), message=broadcast_text, random_id=random.randint(1, 2_000_000_000))
            count += 1
        except Exception:
            pass
    add_log(f"Массовая рассылка выполнена {sender_fmt}. Охвачено чатов: {count}")
    return count


# =====================================================================
# 6. ПОМОЩНИКИ ВК, КЛАВИАТУРЫ И СПРАВОЧНИК
# =====================================================================
async def format_vk_user(uid: int) -> str:
    if uid < 0:
        return f"[club{abs(uid)}|Сообщество {abs(uid)}]"
    now = time.time()
    cached = user_cache.get(uid)
    if cached and (now - cached["ts"] < 3600):
        return cached["label"]
    try:
        users = await bot.api.users.get(user_ids=[uid], fields=["screen_name"])
        if users:
            u = users[0]
            full_name = f"{u.first_name} {u.last_name}".strip()
            screen = f" (@{u.screen_name})" if getattr(u, "screen_name", None) else ""
            label = f"[id{uid}|{full_name}]{screen}"
            user_cache[uid] = {"label": label, "ts": now}
            return label
    except Exception:
        pass
    return f"[id{uid}|ID {uid}]"


def extract_mention_id(raw_token: str) -> int | None:
    token = raw_token.strip()
    if token.startswith("[id") and "|" in token and token.endswith("]"):
        inner = token[3:].split("|", 1)[0]
        if inner.isdigit():
            return int(inner)
    if token.startswith("[club") and "|" in token and token.endswith("]"):
        inner = token[5:].split("|", 1)[0]
        if inner.isdigit():
            return -int(inner)
    return None


async def resolve_target(message: Message, args: list):
    if message.reply_message and getattr(message.reply_message, "from_id", None):
        return int(message.reply_message.from_id), args
    if not args:
        return None, []

    target_raw = args[0].strip()
    remaining_args = args[1:]

    mention_id = extract_mention_id(target_raw)
    if mention_id:
        return mention_id, remaining_args

    cleaned = (
        target_raw
        .replace("https://vk.com/", "")
        .replace("http://vk.com/", "")
        .replace("https://vk.ru/", "")
        .replace("http://vk.ru/", "")
        .replace("vk.com/", "")
        .replace("vk.ru/", "")
        .replace("m.vk.com/", "")
        .lstrip("@")
        .strip()
    )

    if cleaned.lower().startswith("id") and cleaned[2:].isdigit():
        return int(cleaned[2:]), remaining_args

    if cleaned.isdigit():
        return int(cleaned), remaining_args

    if cleaned:
        try:
            user_info = await bot.api.users.get(user_ids=[cleaned])
            if user_info:
                return int(user_info[0].id), remaining_args
        except Exception:
            pass

    return None, args


def get_chat_inline_keyboard() -> str:
    kb = Keyboard(one_time=False, inline=True)
    kb.add(Text("🎁 Бонус"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("📊 Стата"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🏆 Топ"), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Text("🪙 Монетка"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🎲 Кубик"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🎰 Слоты"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("🎡 Рулетка"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("💼 Работа"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("⚙️ Настройка чата"), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Text("👑 Админы"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("📜 Правила"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("📖 Команды"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def get_chat_customize_keyboard(chat_data: dict) -> str:
    kb = Keyboard(one_time=False, inline=True)
    kb.add(Text("👋 Приветствие"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("📜 Правила чата"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("🏷 Название чата"), color=KeyboardButtonColor.PRIMARY)
    kb.add(Text("🌐 Ссылка проекта"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text(f"🛡 Антиспам: {'ВКЛ' if chat_data.get('antispam') else 'ВЫКЛ'}"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text(f"🔠 Антикапс: {'ВКЛ' if chat_data.get('caps_guard') else 'ВЫКЛ'}"), color=KeyboardButtonColor.SECONDARY)
    kb.row()
    kb.add(Text(f"🌊 Антифлуд: {'ВКЛ' if chat_data.get('antiflood') else 'ВЫКЛ'}"), color=KeyboardButtonColor.SECONDARY)
    kb.add(Text("🔓 Открыть чат" if chat_data.get("locked") else "🔒 Закрыть чат"), color=KeyboardButtonColor.NEGATIVE)
    kb.row()
    kb.add(Text("👑 Забрать 100 lvl"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("🔙 Главное меню"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def get_ban_days_keyboard(token_key: str) -> str:
    kb = Keyboard(one_time=True, inline=True)
    kb.add(Text(f"/бан_срок {token_key} 1"), color=KeyboardButtonColor.NEGATIVE)
    kb.add(Text(f"/бан_срок {token_key} 3"), color=KeyboardButtonColor.NEGATIVE)
    kb.add(Text(f"/бан_срок {token_key} 7"), color=KeyboardButtonColor.NEGATIVE)
    kb.row()
    kb.add(Text(f"/бан_срок {token_key} 14"), color=KeyboardButtonColor.NEGATIVE)
    kb.add(Text(f"/бан_срок {token_key} 30"), color=KeyboardButtonColor.NEGATIVE)
    kb.add(Text(f"/бан_срок {token_key} 90"), color=KeyboardButtonColor.NEGATIVE)
    kb.row()
    kb.add(Text(f"/бан_срок {token_key} 180"), color=KeyboardButtonColor.NEGATIVE)
    kb.add(Text(f"/бан_срок {token_key} 360"), color=KeyboardButtonColor.NEGATIVE)
    kb.row()
    kb.add(Text(f"/бан_отмена {token_key}"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def get_duel_keyboard(token_key: str) -> str:
    kb = Keyboard(one_time=True, inline=True)
    kb.add(Text(f"/дуэль_принять {token_key}"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text(f"/дуэль_отмена {token_key}"), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def build_full_help_with_ranks(proj_name: str) -> str:
    return (
        f"📖 ═══ ПОЛНЫЙ СПРАВОЧНИК И РАНГИ ({proj_name}) ═══ 📖\n\n"
        "⚙️ **Настройка чата кнопками:** нажми `⚙️ Настройка чата` (или `/настройка`), чтобы менять приветствие, правила, название, ссылку и защиты прямо в чате.\n"
        "👑 **ТОЛЬКО ДЛЯ ВЛАДЕЛЬЦА БОТА (`897082738`):**\n"
        "• `/скачатьбд` / `/бэкап` — скачать `bot_database.json` файлом\n"
        "• `/загрузитьбд` — загрузить базу `.json` файлом\n"
        "• `/чатылог [peer_id/№] [кол-во]` — что писали в беседах\n"
        "• `/взаимодействия [кол-во]` — что писали и отправляли боту\n"
        "• `/вип @цель [1д..10лет]` / `/снятьвип @цель` — VIP (1 день – 10 лет)\n"
        "• `/выдатьмонеты @цель [сумма]` / `/снятьмонеты @цель [сумма]` — выдача/снятие монет\n"
        "• `/создатькод [global|nick_roles|all]` — бесконечный код (`1 код = ∞ чатов`)\n\n"
        "💰 **Игры и бонус:**\n"
        "• `/бонус` — от `1 000 000` до `50 000 000` монет\n"
        "• `/работа`, `/перевод @цель [сумма]`, `/топ`, `/монетка`, `/кубик`, `/слоты`, `/рулетка`, `/дуэль`\n\n"
        "🛡 **Модерация и ранги (`0..100`):**\n"
        "• `/бан @цель` (или реплаем `/бан`) — кнопки выбора срока бана (`1–360` дней)\n"
        "• `/анбан`, `/мут`, `/анмут`, `/кик`, `/варн`, `/анварн`, `/сетадмин`, `/сник`, `/код [ключ]`\n"
        "• Сетевой пул `/г...` и `/g...`: `/гбан`, `/гунбан`, `/грассылка`, `/гчаты`, `/гтоп`"
    )


# =====================================================================
# 7. ЛИЧНЫЕ СООБЩЕНИЯ (ПАНЕЛЬ ВЛАДЕЛЬЦА + БЭКАП/СКАЧАТЬ/ЛОГИ)
# =====================================================================
def get_private_keyboard(uid: int) -> str:
    is_main = is_bot_owner(uid)
    has_broadcast = (is_main or uid in db["broadcast_admins"])
    kb = Keyboard(one_time=False, inline=False)
    kb.add(Text("🌐 Ссылка проекта"), color=KeyboardButtonColor.POSITIVE)

    if has_broadcast:
        kb.row()
        kb.add(Text("📢 Рассылка по чатам"), color=KeyboardButtonColor.NEGATIVE)
        kb.add(Text("📜 Логи рассылок"), color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Text("➕ Админ рассылки"), color=KeyboardButtonColor.POSITIVE)
        kb.add(Text("👥 Список рассылки"), color=KeyboardButtonColor.PRIMARY)

    if is_main:
        kb.row()
        kb.add(Text("📥 Скачать БД"), color=KeyboardButtonColor.POSITIVE)
        kb.add(Text("📤 Загрузить БД"), color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Text("💬 Что писали в чатах"), color=KeyboardButtonColor.SECONDARY)
        kb.add(Text("🤖 Что писали боту"), color=KeyboardButtonColor.SECONDARY)
        kb.row()
        kb.add(Text("🎲 Код /г... (∞ чатов)"), color=KeyboardButtonColor.POSITIVE)
        kb.add(Text("🎭 Код Ники/Роли (∞ чатов)"), color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(Text("⭐ Полный код (∞ чатов)"), color=KeyboardButtonColor.PRIMARY)
        kb.add(Text("📦 Пачка кодов (5 шт)"), color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Text("📋 Все коды"), color=KeyboardButtonColor.SECONDARY)
        kb.add(Text("🗑 Удалить код"), color=KeyboardButtonColor.NEGATIVE)

    return kb.get_json()


@bot.on.private_message()
async def private_handler(message: Message):
    uid = int(message.from_id)
    text = (message.text or "").strip()
    low = text.lower()
    state = user_states.get(uid)
    is_main = is_bot_owner(uid)
    has_broadcast = (is_main or uid in db["broadcast_admins"])

    if text:
        record_bot_interaction(uid, uid, text)

    if (state == "wait_upload_db" or low.startswith(("/загрузитьбд", "/loaddb", "/восстановитьбд"))) and is_main:
        if message.attachments or (message.reply_message and message.reply_message.attachments):
            user_states.pop(uid, None)
            _, res_txt = await restore_db_from_message(message)
            await message.answer(res_txt, keyboard=get_private_keyboard(uid))
            return
        if low.startswith(("/загрузитьбд", "/loaddb", "/восстановитьбд")):
            user_states[uid] = "wait_upload_db"
            await message.answer(
                "📤 Прикрепи файл `bot_database.json` следующим сообщением (или ответь командой на документ).",
                keyboard=get_private_keyboard(uid),
            )
            return

    if not text:
        return

    if low in ["/start", "начать", "меню", "🔙 главное меню", "❌ отмена"]:
        user_states.pop(uid, None)
        await message.answer(
            f"👑 ═══ {DEFAULT_PROJECT_NAME} — ПАНЕЛЬ ВЛАДЕЛЬЦА ═══ 👑\n\n"
            f"• Бесконечные коды: `1 код = ∞ чатов` (активных чатов: `{count_active_pool_chats()}`)\n"
            "• БД (только владельцу): `/скачатьбд`, `/бэкап`, `/загрузитьбд`\n"
            "• Мониторинг (только владельцу): `/чатылог`, `/взаимодействия`",
            keyboard=get_private_keyboard(uid),
        )
        return

    if text in ["📥 Скачать БД", "💾 Бэкап БД"] or low in ("/скачатьбд", "/downloaddb", "/бэкап", "/backup"):
        if not is_main:
            await message.answer("❌ Команда доступна только владельцу бота.")
            return
        ok, info_err = await send_db_file(uid, caption="💾 База данных bot_database.json")
        if not ok:
            await message.answer(f"❌ {info_err}", keyboard=get_private_keyboard(uid))
        return

    if text == "📤 Загрузить БД":
        if not is_main:
            return
        user_states[uid] = "wait_upload_db"
        await message.answer(
            "📤 Прикрепи `.json` файл базы данных к следующему сообщению:",
            keyboard=Keyboard(one_time=False, inline=True).add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE).get_json(),
        )
        return

    if text == "💬 Что писали в чатах" or low.startswith("/чатылог"):
        if not is_main:
            return
        parts = text.split()
        limit = 15
        filter_peer = None
        if len(parts) >= 2 and parts[1].isdigit():
            val = int(parts[1])
            if val > 100000:
                filter_peer = val
                if len(parts) >= 3 and parts[2].isdigit():
                    limit = max(1, min(40, int(parts[2])))
            else:
                limit = max(1, min(40, val))
        entries = db.get("chat_messages_log", [])
        if filter_peer is not None:
            entries = [e for e in entries if int(e.get("peer_id", 0)) == filter_peer]
        recent = entries[-limit:]
        if not recent:
            await message.answer("💬 Записей сообщений из чатов пока нет.", keyboard=get_private_keyboard(uid))
            return
        lines = [f"[{e['ts']}] чат `{e['peer_id']}` | `@id{e['uid']}`: {e['text']}" for e in recent]
        await message.answer("💬 **ПОСЛЕДНИЕ СООБЩЕНИЯ В ЧАТАХ:**\n\n" + "\n".join(lines), keyboard=get_private_keyboard(uid))
        return

    if text == "🤖 Что писали боту" or low.startswith("/взаимодействия"):
        if not is_main:
            return
        parts = text.split()
        limit = int(parts[1]) if (len(parts) >= 2 and parts[1].isdigit()) else 15
        limit = max(1, min(40, limit))
        recent = db.get("bot_interactions_log", [])[-limit:]
        if not recent:
            await message.answer("🤖 Записей обращений к боту пока нет.", keyboard=get_private_keyboard(uid))
            return
        lines = [f"[{e['ts']}] `@id{e['uid']}` (`{e['peer_id']}`): {e['text']}" for e in recent]
        await message.answer("🤖 **ЧТО ПИСАЛИ / ОТПРАВЛЯЛИ БОТУ:**\n\n" + "\n".join(lines), keyboard=get_private_keyboard(uid))
        return

    if text in ["🎲 Код /г... (1 чат)", "🎲 Код /г... (∞ чатов)"]:
        if not is_main:
            return
        code = generate_random_code("global", uid)
        await message.answer(
            f"🎲 **Бесконечный код для пула `/г...` и `/g...` создан!**\n• Ключ: `{code}` (`∞` чатов)\n• В беседе: `/код {code}`",
            keyboard=get_private_keyboard(uid),
        )
        return

    if text in ["🎭 Код Ники/Роли (1 чат)", "🎭 Код Ники/Роли (∞ чатов)"]:
        if not is_main:
            return
        code = generate_random_code("nick_roles", uid)
        await message.answer(
            f"🎭 **Бесконечный код для пула «Ники и роли» создан!**\n• Ключ: `{code}` (`∞` чатов)\n• В беседе: `/код {code}`",
            keyboard=get_private_keyboard(uid),
        )
        return

    if text in ["⭐ Полный код (1 чат)", "⭐ Полный код (∞ чатов)"]:
        if not is_main:
            return
        code = generate_random_code("all", uid)
        await message.answer(
            f"⭐ **Полный бесконечный код (Оба пула) создан!**\n• Ключ: `{code}` (`∞` чатов)\n• В беседе: `/код {code}`",
            keyboard=get_private_keyboard(uid),
        )
        return

    if text == "📦 Пачка кодов (5 шт)":
        if not is_main:
            return
        generated = [generate_random_code("all", uid) for _ in range(5)]
        formatted = "\n".join([f"{idx + 1}. `/код {c}` (`∞` чатов)" for idx, c in enumerate(generated)])
        await message.answer(
            f"📦 **Выпущена пачка из 5 бесконечных кодов (`1 код = ∞ чатов`):**\n\n{formatted}",
            keyboard=get_private_keyboard(uid),
        )
        return

    if text in ["📋 Все коды", "📦 Все коды"]:
        if not is_main:
            return
        codes = db.get("pool_codes", {})
        if not codes:
            await message.answer("📋 Выданных кодов пока нет.", keyboard=get_private_keyboard(uid))
            return
        lines = []
        type_labels = {"global": "🌐 /г... и /g...", "nick_roles": "🎭 Ники и роли", "all": "⭐ Оба пула"}
        for code, info in list(codes.items())[-30:]:
            used = len(info.get("chats", []))
            t_lbl = type_labels.get(info.get("type", "global"), info.get("type"))
            lines.append(f"• `{code}` | {t_lbl} | чатов: `{used}` (`∞`)")
        await message.answer(
            f"📋 **БЕСКОНЕЧНЫЕ КОДЫ (активных чатов в пуле: `{count_active_pool_chats()}`):**\n\n" + "\n".join(lines),
            keyboard=get_private_keyboard(uid),
        )
        return

    if text == "🗑 Удалить код":
        if not is_main:
            return
        user_states[uid] = "wait_del_code"
        await message.answer(
            "🗑 Введите код, который нужно отозвать и удалить:",
            keyboard=Keyboard(one_time=False, inline=True).add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE).get_json(),
        )
        return

    if text in ["➕ Админ рассылки", "➕ Выдать доступ к рассылке"]:
        if not has_broadcast:
            return
        user_states[uid] = "wait_add_adm"
        await message.answer(
            "✍️ Введите цифровой VK ID или @ник пользователя для выдачи доступа к глобальной рассылке:",
            keyboard=Keyboard(one_time=False, inline=True).add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE).get_json(),
        )
        return

    if text == "📜 Логи рассылок":
        if not has_broadcast:
            return
        broadcast_logs = [log for log in db["logs"] if "рассылка" in log.lower() or "рассылки" in log.lower()]
        recent = "\n".join(broadcast_logs[-10:]) if broadcast_logs else "📋 Логов рассылок пока нет."
        await message.answer(f"📜 **ИСТОРИЯ ОФИЦИАЛЬНЫХ РАССЫЛОК:**\n\n{recent}", keyboard=get_private_keyboard(uid))
        return

    if text in ["👥 Список рассылки", "👥 Список админов рассылки"]:
        if not has_broadcast:
            return
        lines = [f"• {await format_vk_user(aid)}" for aid in db["broadcast_admins"]]
        await message.answer(
            "👥 **ДОВЕРЕННЫЕ АДМИНЫ РАССЫЛКИ:**\n\n" + ("\n".join(lines) if lines else "Список пуст."),
            keyboard=get_private_keyboard(uid),
        )
        return

    if text in ["📢 Рассылка по чатам", "📢 Начать рассылку по чатам"]:
        if not has_broadcast:
            return
        user_states[uid] = "wait_broadcast"
        await message.answer(
            "✍️ Введите текст для массовой отправки по всем подключённым беседам:",
            keyboard=Keyboard(one_time=False, inline=True).add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE).get_json(),
        )
        return

    if text in ["🌐 Ссылка проекта", "🌐 Сайт проекта"]:
        await message.answer(f"🌐 Ссылка проекта: {DEFAULT_PROJECT_URL}", keyboard=get_private_keyboard(uid))
        return

    if is_main and low.startswith(("/создатькод", "/выдатькод", "/genpool")):
        parts = text.split()
        p_type = parts[1].lower() if len(parts) > 1 else "all"
        if p_type not in ("global", "nick_roles", "all"):
            await message.answer("❌ Формат: `/создатькод [global|nick_roles|all]`", keyboard=get_private_keyboard(uid))
            return
        code = generate_random_code(p_type, uid)
        await message.answer(
            f"🎲 **Бесконечный код создан (`∞` бесед):** `{code}` (`{p_type}`)\n• Ввод в чате: `/код {code}`",
            keyboard=get_private_keyboard(uid),
        )
        return

    if state == "wait_del_code" and is_main:
        user_states.pop(uid, None)
        _, msg_del = revoke_pool_code(text)
        await message.answer(msg_del, keyboard=get_private_keyboard(uid))
        return

    if state == "wait_add_adm" and has_broadcast:
        user_states.pop(uid, None)
        tid, _ = await resolve_target(message, [text])
        if tid and tid > 0:
            if tid not in db["broadcast_admins"]:
                db["broadcast_admins"].append(tid)
                save_db(db)
                u_fmt = await format_vk_user(tid)
                add_log(f"Админ {await format_vk_user(uid)} выдал права рассылки для {u_fmt}")
                await message.answer(f"✅ Успешно! {u_fmt} добавлен в список рассыльщиков.", keyboard=get_private_keyboard(uid))
                return
            await message.answer("ℹ️ Этот пользователь уже в списке.", keyboard=get_private_keyboard(uid))
            return
        await message.answer("❌ Ошибка: укажите корректный VK ID или @ник.", keyboard=get_private_keyboard(uid))
        return

    if state == "wait_broadcast" and has_broadcast:
        user_states.pop(uid, None)
        count = await run_global_broadcast(uid, text)
        await message.answer(f"🚀 **Рассылка успешно завершена!**\n• Доставлено в беседы: `{count}`", keyboard=get_private_keyboard(uid))
        return

    await message.answer(
        "👋 Используй кнопки ниже или `/создатькод [global|nick_roles|all]` для выпуска бесконечных кодов (`1 код = ∞ чатов`).",
        keyboard=get_private_keyboard(uid),
    )


# =====================================================================
# 8. РАБОТА В БЕСЕДАХ
# =====================================================================
@bot.on.chat_message()
async def chat_handler(message: Message):
    peer_id = int(message.peer_id)
    uid_int = int(message.from_id or 0)
    if uid_int <= 0:
        return

    uid_str = str(uid_int)
    chat_id_num = get_chat_id_from_peer(message)
    chat_data = get_chat_data(peer_id)

    is_new_chat = peer_id not in db["global_chats"]
    if is_new_chat:
        db["global_chats"].append(peer_id)

    chat_title, owner_id = await resolve_chat_owner(peer_id, chat_data)
    save_db(db)

    if message.text and message.text.strip():
        record_chat_message(peer_id, uid_int, message.text.strip())

    # --- Событие входа / приглашения ---
    if message.action and message.action.type == "chat_invite_user":
        j_id = message.action.member_id
        group_id = getattr(message, "group_id", None)
        is_bot_invited = (group_id and j_id == -group_id) or (j_id is not None and j_id < 0)

        if is_bot_invited or (is_new_chat and not chat_data.get("welcomed")):
            chat_data["welcomed"] = True
            save_db(db)
            await message.answer(build_welcome_guide(chat_data), keyboard=get_chat_inline_keyboard())
            return

        if j_id and (str(j_id) in db["global_bans"] or str(j_id) in chat_data.get("bans", {})):
            if chat_id_num:
                try:
                    await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=j_id)
                    await message.answer(f"⛔ Вход заблокирован! У {await format_vk_user(j_id)} активный бан.")
                except Exception:
                    pass
            return

        if j_id and j_id > 0 and chat_data.get("greeting_text"):
            try:
                greet = chat_data["greeting_text"].replace("{user}", await format_vk_user(j_id)).replace("{chat}", chat_title)
                await message.answer(greet)
            except Exception:
                pass
        return

    # --- Проверка активного мута ---
    if uid_str in chat_data.get("mutes", {}):
        if time.time() < chat_data["mutes"][uid_str]:
            try:
                await bot.api.messages.delete(
                    peer_id=peer_id,
                    conversation_message_ids=[message.conversation_message_id],
                    delete_for_all=True,
                )
            except Exception:
                pass
            return
        else:
            del chat_data["mutes"][uid_str]
            save_db(db)

    raw_text = (message.text or "").strip()
    is_main_owner = is_bot_owner(uid_int)

    if is_main_owner and raw_text.lower().startswith(("/загрузитьбд", "/loaddb", "/восстановитьбд")):
        _, res_txt = await restore_db_from_message(message)
        return await message.answer(res_txt)

    if not raw_text:
        return

    clean_text = raw_text.lower()
    proj_name = chat_data.get("project_name", DEFAULT_PROJECT_NAME)
    proj_url = chat_data.get("project_url", DEFAULT_PROJECT_URL)
    user_lvl = get_user_level_by_id(uid_int, chat_data, owner_id)

    # --- Ожидание ввода настройки чата кнопками ---
    setup_key = (peer_id, uid_int)
    pending_setup = chat_setup_states.get(setup_key)
    if pending_setup and (time.time() - pending_setup.get("ts", 0) < 300):
        if clean_text in ("отмена", "/отмена", "🔙 главное меню"):
            chat_setup_states.pop(setup_key, None)
            return await message.answer("✅ Настройка отменена.", keyboard=get_chat_customize_keyboard(chat_data))
        act = pending_setup.get("action")
        chat_setup_states.pop(setup_key, None)
        if act == "set_greeting":
            chat_data["greeting_text"] = raw_text[:500]
            save_db(db)
            return await message.answer(f"✅ Приветствие сохранено:\n{chat_data['greeting_text']}", keyboard=get_chat_customize_keyboard(chat_data))
        if act == "set_rules":
            chat_data["rules_text"] = raw_text[:2000]
            save_db(db)
            return await message.answer("✅ Правила беседы обновлены!", keyboard=get_chat_customize_keyboard(chat_data))
        if act == "set_project_name":
            chat_data["project_name"] = raw_text[:50]
            save_db(db)
            return await message.answer(f"✅ Название проекта обновлено: **{chat_data['project_name']}**", keyboard=get_chat_customize_keyboard(chat_data))
        if act == "set_project_url":
            chat_data["project_url"] = raw_text[:200]
            save_db(db)
            return await message.answer(f"✅ Ссылка проекта обновлена: {chat_data['project_url']}", keyboard=get_chat_customize_keyboard(chat_data))

    # --- Закрытый чат (/лок) ---
    if chat_data.get("locked") and user_lvl < 45 and not clean_text.startswith("/"):
        try:
            await bot.api.messages.delete(
                peer_id=peer_id,
                conversation_message_ids=[message.conversation_message_id],
                delete_for_all=True,
            )
        except Exception:
            pass
        return

    # --- Антифлуд ---
    if chat_data.get("antiflood") and user_lvl < 45:
        now_t = time.time()
        key_fl = (peer_id, uid_int)
        stamps = [t for t in flood_tracker.get(key_fl, []) if now_t - t < 6.0]
        stamps.append(now_t)
        flood_tracker[key_fl] = stamps
        if len(stamps) >= 5:
            try:
                await bot.api.messages.delete(
                    peer_id=peer_id,
                    conversation_message_ids=[message.conversation_message_id],
                    delete_for_all=True,
                )
            except Exception:
                pass
            return

    # --- Антиспам ссылок ---
    if chat_data.get("antispam") and user_lvl < 45:
        if any(bad in clean_text for bad in ["http://", "https://", "t.me/", "vk.me/", "discord.gg/"]):
            try:
                await bot.api.messages.delete(
                    peer_id=peer_id,
                    conversation_message_ids=[message.conversation_message_id],
                    delete_for_all=True,
                )
                await message.answer(f"🛡 Антиспам удалил ссылку от {await format_vk_user(uid_int)}.")
            except Exception:
                pass
            return

    # --- Антиреклама ---
    if chat_data.get("antiads") and user_lvl < 45:
        if any(tag in clean_text for tag in ["[club", "[public", "vk.com/join", "vk.me/", "t.me/"]):
            try:
                await bot.api.messages.delete(
                    peer_id=peer_id,
                    conversation_message_ids=[message.conversation_message_id],
                    delete_for_all=True,
                )
                await message.answer(f"🚫 Антиреклама удалила промо-сообщение от {await format_vk_user(uid_int)}.")
            except Exception:
                pass
            return

    # --- Антикапс ---
    if chat_data.get("caps_guard") and user_lvl < 45 and len(raw_text) >= 10:
        letters = [c for c in raw_text if c.isalpha()]
        if letters and (sum(1 for c in letters if c.isupper()) / len(letters)) >= 0.8:
            try:
                await bot.api.messages.delete(
                    peer_id=peer_id,
                    conversation_message_ids=[message.conversation_message_id],
                    delete_for_all=True,
                )
                await message.answer(f"🔠 Антикапс удалил сообщение от {await format_vk_user(uid_int)}.")
            except Exception:
                pass
            return

    for mention in ["[club" + str(message.group_id) + "|@", "@club", "@public"]:
        if mention in clean_text:
            parts = raw_text.split(" ", 1)
            if len(parts) > 1:
                raw_text = parts[1].strip()
                clean_text = raw_text.lower()

    if clean_text.startswith("+реп"):
        rem_part = raw_text[4:].strip().split()
        raw_text = ("/реп " + " ".join(rem_part)).strip()
        clean_text = raw_text.lower()

    # --- Кнопки настройки беседы ---
    if clean_text in ["⚙️ настройка чата", "настройка чата", "/настройка", "/setup"]:
        cur_greet = chat_data.get("greeting_text") or "Не задано"
        return await message.answer(
            f"⚙️ ═══ НАСТРОЙКА БЕСЕДЫ ({proj_name}) ═══ ⚙️\n\n"
            f"• Ваш уровень: `{user_lvl} lvl`\n"
            f"• Приветствие: `{cur_greet[:80]}`\n"
            f"• Защиты: Антиспам `{'ВКЛ' if chat_data.get('antispam') else 'ВЫКЛ'}` | "
            f"Антикапс `{'ВКЛ' if chat_data.get('caps_guard') else 'ВЫКЛ'}` | "
            f"Антифлуд `{'ВКЛ' if chat_data.get('antiflood') else 'ВЫКЛ'}`\n\n"
            "Нажми кнопку ниже, чтобы изменить параметр или забрать `100 lvl` владельца чата:",
            keyboard=get_chat_customize_keyboard(chat_data),
        )

    if clean_text in ["👋 приветствие", "📜 правила чата", "🏷 название чата", "🌐 ссылка проекта"]:
        if user_lvl < 70 and not is_main_owner:
            return await message.answer(f"❌ Для изменения настроек чата нужен `70+ lvl` (у вас `{user_lvl} lvl`). Нажми `👑 Забрать 100 lvl`, если ты админ/создатель чата ВК.")
        action_map = {
            "👋 приветствие": ("set_greeting", "✍️ Напиши следующим сообщением текст приветствия (можно использовать `{user}` и `{chat}`):"),
            "📜 правила чата": ("set_rules", "✍️ Напиши следующим сообщением новые правила беседы:"),
            "🏷 название чата": ("set_project_name", "✍️ Напиши следующим сообщением новое название проекта для этой беседы:"),
            "🌐 ссылка проекта": ("set_project_url", "✍️ Напиши следующим сообщением ссылку проекта (например `https://vk.com/...`):"),
        }
        act_name, prompt_txt = action_map[clean_text]
        chat_setup_states[setup_key] = {"action": act_name, "ts": time.time()}
        return await message.answer(prompt_txt)

    if clean_text.startswith(("🛡 антиспам:", "🔠 антикапс:", "🌊 антифлуд:")):
        if user_lvl < 70 and not is_main_owner:
            return await message.answer(f"❌ Требуется `70+ lvl` (у вас `{user_lvl} lvl`).")
        if clean_text.startswith("🛡 антиспам:"):
            chat_data["antispam"] = not bool(chat_data.get("antispam"))
            status_str = f"🛡 Антиспам: {'🟢 ВКЛ' if chat_data['antispam'] else '🔴 ВЫКЛ'}"
        elif clean_text.startswith("🔠 антикапс:"):
            chat_data["caps_guard"] = not bool(chat_data.get("caps_guard"))
            status_str = f"🔠 Антикапс: {'🟢 ВКЛ' if chat_data['caps_guard'] else '🔴 ВЫКЛ'}"
        else:
            chat_data["antiflood"] = not bool(chat_data.get("antiflood"))
            status_str = f"🌊 Антифлуд: {'🟢 ВКЛ' if chat_data['antiflood'] else '🔴 ВЫКЛ'}"
        save_db(db)
        return await message.answer(f"✅ {status_str}", keyboard=get_chat_customize_keyboard(chat_data))

    if clean_text in ["🔒 закрыть чат", "🔓 открыть чат"]:
        if user_lvl < 45 and not is_main_owner:
            return await message.answer(f"❌ Требуется `45+ lvl` (у вас `{user_lvl} lvl`).")
        chat_data["locked"] = not bool(chat_data.get("locked"))
        save_db(db)
        return await message.answer(
            "🔒 Беседа закрыта для участников `<45 lvl`." if chat_data["locked"] else "🔓 Беседа снова открыта.",
            keyboard=get_chat_customize_keyboard(chat_data),
        )

    if clean_text in ["👑 забрать 100 lvl", "забрать 100 lvl"]:
        is_vk_owner, is_vk_admin = await get_vk_chat_member_flags(peer_id, uid_int)
        if not (is_vk_owner or is_vk_admin or is_main_owner):
            return await message.answer("⛔ Забрать `100 lvl` по кнопке могут только создатель или администратор беседы ВК.")
        sync_owner_member(chat_data, int(uid_int))
        save_db(db)
        return await message.answer(
            f"👑 {await format_vk_user(uid_int)} получил `100 lvl` (Владелец чата)! Теперь доступны все настройки и модерация.",
            keyboard=get_chat_customize_keyboard(chat_data),
        )

    # --- Кнопки и быстрые фразы ---
    quick_buttons = {
        "🎁 бонус": "/бонус",
        "📊 стата": "/стата",
        "🏆 топ": "/топ",
        "🪙 монетка": "/монетка 100000 орёл",
        "🎲 кубик": "/кубик 100000 6",
        "🎰 слоты": "/слоты 100000",
        "🎡 рулетка": "/рулетка 100000 красное",
        "💼 работа": "/работа",
        "👑 админы": "/админс",
        "📜 правила": "/правила",
        "📖 команды": "/хелп",
        "хелп": "/хелп",
        "help": "/хелп",
        "команды": "/хелп",
    }
    if clean_text in quick_buttons:
        raw_text = quick_buttons[clean_text]
        clean_text = raw_text.lower()

    if clean_text in ["/menu", "/panel", "/панель", "/меню", "меню", "панель", "🔙 главное меню"]:
        return await message.answer(f"👑 ═══ ПАНЕЛЬ УПРАВЛЕНИЯ — {proj_name} ═══ 👑", keyboard=get_chat_inline_keyboard())

    if clean_text in ["🔑 код пула", "код пула"]:
        return await message.answer(
            "🔑 ═══ АКТИВАЦИЯ БЕСКОНЕЧНОГО КОДА ═══ 🔑\n\n"
            "Введите команду: `/код [ВАШ_КЛЮЧ]` или `/активировать [ВАШ_КЛЮЧ]`\n"
            "• Формат ключа: `1 код = ∞ бесед`.\n"
            "• Пул `/г...` и `/g...` открывает сетевые команды.\n"
            "• Отдельный пул «Ники и роли» открывает ники, ранги и настройку ролей.",
            keyboard=get_chat_inline_keyboard(),
        )

    if clean_text in ["📦 статус пула", "статус пула", "/пул", "/pool"]:
        g_ok = has_global_pool(chat_data)
        nr_ok = has_nick_roles_pool(chat_data)
        g_code = chat_data["pools"].get("global_code") or "—"
        nr_code = chat_data["pools"].get("nick_roles_code") or "—"
        return await message.answer(
            f"📦 ═══ СТАТУС ПУЛОВ БЕСЕДЫ ═══ 📦\n\n"
            f"• Пул `/г...` и `/g...`: {'🟢 Активен' if g_ok else '🔴 Не активирован'} (`{g_code}`)\n"
            f"• Отдельный пул «Ники и роли»: {'🟢 Активен' if nr_ok else '🔴 Не активирован'} (`{nr_code}`)\n"
            f"• Формат ключей: `1 код = ∞ бесед`\n"
            f"• Активных чатов в общем пуле: `{count_active_pool_chats()}/{MAX_ACTIVE_POOL_CHATS}`",
            keyboard=get_chat_inline_keyboard(),
        )

    if clean_text in ["🌐 ссылка", "🌐 сайт", "ссылка", "сайт", "/link", "/ссылка"]:
        return await message.answer(f"🌐 Ссылка проекта **{proj_name}**: {proj_url}", keyboard=get_chat_inline_keyboard())

    if clean_text in ["правила", "/rules", "/правила"]:
        return await message.answer(chat_data.get("rules_text", DEFAULT_RULES), keyboard=get_chat_inline_keyboard())

    if clean_text in ["🛠 модерация", "модерация"]:
        return await message.answer(
            "🛠 ═══ ШПАРГАЛКА МОДЕРАЦИИ ═══ 🛠\n\n"
            "• `/бан @цель` (или реплаем `/бан`) — кнопки срока бана `1–360` дней\n"
            "• `15+ lvl`: `/заметка`, `/удзаметку`\n"
            "• `45+ lvl`: `/мут`, `/анмут`, `/варн`, `/варны`, `/анварн`, `/кик`, `/очистить`, `/закреп`, `/откреп`, `/лок`, `/анлок`\n"
            "• `55+ lvl`: `/анбан`, `/банлист`, `/чекбан`, `/снятьвсеварны`, `/статачата`\n"
            "• `60+ lvl`: `/сник`, `/делник` *(пул)*, `/сетлидер`, `/снятлидера`, `/репорты`, `/ответить`, `/созыв`\n"
            "• `70+ lvl`: `/сетадмин`, `/снятьроль` *(пул)*, `/повысить`, `/понизить`, `/выговор`, `/выговоры`, `/снятьвыговор`, `/сетправила`, `/сетпривет`, `/размутвсех`, `/антиспам`, `/антикапс`, `/антифлуд`, `/антиреклама`, `/логи`\n"
            "• `90+ lvl`: `/новаяроль`, `/удалитьроль`, `/сбросролей` *(пул)*, `/сетназвание`, `/сетссылка`, `/сетвладелец`, `/сбросвладельца`",
            keyboard=get_chat_inline_keyboard(),
        )

    if clean_text in ["📋 роли", "/roles", "/роли", "роли"]:
        r_list = f"📋 ═══ ИЕРАРХИЯ ДОЛЖНОСТЕЙ ({proj_name}) ═══ 📋\n\n"
        chat_roles = chat_data.get("role_names", {})
        if not chat_roles:
            return await message.answer("📋 Список ролей в этом чате пуст.", keyboard=get_chat_inline_keyboard())
        for lvl in sorted((int(k) for k in chat_roles.keys()), reverse=True):
            title = chat_roles.get(str(lvl)) or chat_roles.get(lvl) or "Роль"
            r_list += f"• `{lvl} lvl` ➔ **{title}**\n"
        return await message.answer(r_list, keyboard=get_chat_inline_keyboard())

    if not clean_text.startswith("/"):
        return

    record_bot_interaction(peer_id, uid_int, raw_text)

    args = raw_text[1:].split()
    cmd = args[0].lower() if args else ""
    params = args[1:] if len(args) > 1 else []

    # =================================================================
    # СПРАВКА (/хелп, /help, /помощь, /команды)
    # =================================================================
    if cmd in ["хелп", "help", "помощь", "cmds", "команды"]:
        return await message.answer(build_full_help_with_ranks(proj_name), keyboard=get_chat_inline_keyboard())

    # =================================================================
    # КОМАНДЫ ВЛАДЕЛЬЦА БОТА: СКАЧАТЬ БД И МОНИТОРИНГ ЧАТОВ/БОТА
    # =================================================================
    if cmd in ("скачатьбд", "downloaddb", "бэкап", "backup"):
        if not is_main_owner:
            return await message.answer("❌ Скачивать и делать бэкап БД может только владелец бота.")
        ok, info_err = await send_db_file(uid_int, caption=f"💾 База данных bot_database.json (из чата {peer_id})")
        if ok:
            return await message.answer("📩 Файл `bot_database.json` отправлен тебе в личные сообщения!")
        return await message.answer(f"❌ Не удалось отправить файл БД: `{info_err}`")

    elif cmd in ("чатылог", "взаимодействия"):
        if not is_main_owner:
            return await message.answer("❌ Команда доступна только владельцу бота.")
        limit = int(params[0]) if (params and params[0].isdigit()) else 12
        limit = max(1, min(30, limit))
        src = db.get("chat_messages_log", []) if cmd == "чатылог" else db.get("bot_interactions_log", [])
        recent = src[-limit:]
        if not recent:
            return await message.answer("📋 Записей пока нет.")
        lines = [f"[{e['ts']}] чат `{e['peer_id']}` | `@id{e['uid']}`: {e['text']}" for e in recent]
        return await message.answer("📋 **ЖУРНАЛ ЗАПИСЕЙ:**\n\n" + "\n".join(lines))

    # =================================================================
    # БОНУС, ЭКОНОМИКА, VIP (1 ДЕНЬ - 10 ЛЕТ) И МИНИ-ИГРЫ
    # =================================================================
    elif cmd in ("бонус", "bonus"):
        econ = get_econ(uid_int)
        vip_ok, _ = is_vip_active(uid_int)
        gain = random.randint(BONUS_MIN, BONUS_MAX)
        if vip_ok:
            gain *= 2
        with DB_LOCK:
            econ["coins"] = int(econ.get("coins", 0)) + gain
            econ["last_bonus"] = int(time.time())
            save_db(db)
        return await message.answer(
            f"🎁 **Бонус получен!**\n"
            f"• Начислено: `+{format_coins(gain)}` монет" + (" *(VIP x2)*" if vip_ok else "") + "\n"
            f"• Баланс: `{format_coins(econ['coins'])}` монет",
            keyboard=get_chat_inline_keyboard(),
        )

    elif cmd in ("работа", "work"):
        econ = get_econ(uid_int)
        vip_ok, _ = is_vip_active(uid_int)
        pay = random.randint(500_000, 3_500_000) * (2 if vip_ok else 1)
        with DB_LOCK:
            econ["coins"] = int(econ.get("coins", 0)) + pay
            save_db(db)
        return await message.answer(f"💼 Выплата за работу: `+{format_coins(pay)}` монет! Баланс: `{format_coins(econ['coins'])}`.")

    elif cmd in ("перевод", "pay"):
        tid, rem = await resolve_target(message, params)
        if not tid or not rem or not rem[0].isdigit():
            return await message.answer("❌ Формат: `/перевод @ник [сумма]`")
        amt = int(rem[0])
        if amt <= 0 or int(tid) == uid_int:
            return await message.answer("❌ Укажи корректного получателя и сумму больше 0.")
        econ_from = get_econ(uid_int)
        if int(econ_from["coins"]) < amt:
            return await message.answer(f"❌ Недостаточно монет! Баланс: `{format_coins(econ_from['coins'])}`.")
        econ_to = get_econ(int(tid))
        with DB_LOCK:
            econ_from["coins"] = int(econ_from["coins"]) - amt
            econ_to["coins"] = int(econ_to["coins"]) + amt
            save_db(db)
        return await message.answer(f"💸 Переведено `{format_coins(amt)}` монет игроку {await format_vk_user(int(tid))}.")

    elif cmd in ("топ", "топбогачей", "top"):
        items = [(int(k), int(v.get("coins", 0))) for k, v in db.get("economy", {}).items() if isinstance(v, dict) and k.isdigit()]
        items.sort(key=lambda x: x[1], reverse=True)
        if not items:
            return await message.answer("🏆 Топ пока пуст.")
        lines = [
            f"{idx}. {await format_vk_user(u_id)} — `{format_coins(c_val)}` монет" + (" 👑VIP" if is_vip_active(u_id)[0] else "")
            for idx, (u_id, c_val) in enumerate(items[:10], start=1)
        ]
        return await message.answer("🏆 **ТОП ПО МОНЕТАМ:**\n\n" + "\n".join(lines))

    elif cmd in ("монетка", "coin", "flip"):
        if len(params) < 2 or not params[0].isdigit():
            return await message.answer("❌ Формат: `/монетка [ставка] [орёл/решка]`")
        bet = int(params[0])
        side = "орёл" if params[1].lower() in ("орел", "орёл", "eagle", "h", "heads") else ("решка" if params[1].lower() in ("решка", "tails", "t") else "")
        if not side or bet <= 0:
            return await message.answer("❌ Формат: `/монетка [ставка] [орёл/решка]`")
        if int(get_econ(uid_int)["coins"]) < bet:
            return await message.answer("❌ Недостаточно монет.")
        vip_ok, _ = is_vip_active(uid_int)
        drop = side if (vip_ok and random.random() < 0.15) else random.choice(["орёл", "решка"])
        if drop == side:
            win = bet * (2 if vip_ok else 1)
            bal = record_game_result(uid_int, win, True)
            return await message.answer(f"🪙 Выпал(а) **{drop}**! Победа `+{format_coins(win)}` монет! Баланс: `{format_coins(bal)}`.")
        bal = record_game_result(uid_int, -bet, False)
        return await message.answer(f"🪙 Выпал(а) **{drop}**! Проигрыш `-{format_coins(bet)}` монет. Баланс: `{format_coins(bal)}`.")

    elif cmd in ("кубик", "dice", "куб"):
        if len(params) < 2 or not params[0].isdigit() or not params[1].isdigit():
            return await message.answer("❌ Формат: `/кубик [ставка] [1-6]`")
        bet, guess = int(params[0]), int(params[1])
        if bet <= 0 or not (1 <= guess <= 6):
            return await message.answer("❌ Ставка > 0, число от 1 до 6.")
        if int(get_econ(uid_int)["coins"]) < bet:
            return await message.answer("❌ Недостаточно монет.")
        roll = random.randint(1, 6)
        if roll == guess:
            vip_ok, _ = is_vip_active(uid_int)
            win = bet * (8 if vip_ok else 4)
            bal = record_game_result(uid_int, win, True)
            return await message.answer(f"🎲 Выпало **{roll}**! Победа `+{format_coins(win)}` монет! Баланс: `{format_coins(bal)}`.")
        bal = record_game_result(uid_int, -bet, False)
        return await message.answer(f"🎲 Выпало **{roll}** (ставка на **{guess}**). Проигрыш `-{format_coins(bet)}`. Баланс: `{format_coins(bal)}`.")

    elif cmd in ("слоты", "slots", "казино"):
        if not params or not params[0].isdigit():
            return await message.answer("❌ Формат: `/слоты [ставка]`")
        bet = int(params[0])
        if bet <= 0 or int(get_econ(uid_int)["coins"]) < bet:
            return await message.answer("❌ Недостаточно монет или неверная ставка.")
        vip_ok, _ = is_vip_active(uid_int)
        reels = [random.choice(["🍒", "🍋", "💎", "7️⃣", "👑"]) for _ in range(3)]
        line = " | ".join(reels)
        if reels[0] == reels[1] == reels[2]:
            mult = (6 if reels[0] in ("7️⃣", "👑") else 3) * (2 if vip_ok else 1)
            bal = record_game_result(uid_int, bet * mult, True)
            return await message.answer(f"🎰 `[ {line} ]`\nДЖЕКПОТ! `+{format_coins(bet * mult)}` монет! Баланс: `{format_coins(bal)}`.")
        if reels[0] == reels[1] or reels[1] == reels[2]:
            profit = int(bet * (1.0 if vip_ok else 0.5))
            bal = record_game_result(uid_int, profit, True)
            return await message.answer(f"🎰 `[ {line} ]`\nПара! `+{format_coins(profit)}` монет! Баланс: `{format_coins(bal)}`.")
        bal = record_game_result(uid_int, -bet, False)
        return await message.answer(f"🎰 `[ {line} ]`\nПроигрыш `-{format_coins(bet)}`. Баланс: `{format_coins(bal)}`.")

    elif cmd in ("рулетка", "roulette"):
        if len(params) < 2 or not params[0].isdigit():
            return await message.answer("❌ Формат: `/рулетка [ставка] [красное/чёрное/зелёное]`")
        bet = int(params[0])
        c_raw = params[1].lower()
        chosen = "красное" if c_raw in ("красное", "красный", "red", "r") else ("чёрное" if c_raw in ("чёрное", "черное", "чёрный", "черный", "black", "b") else ("зелёное" if c_raw in ("зелёное", "зеленое", "green", "g", "0") else ""))
        if not chosen or bet <= 0 or int(get_econ(uid_int)["coins"]) < bet:
            return await message.answer("❌ Неверная ставка или цвет / недостаточно монет.")
        num = random.randint(0, 36)
        drop = "зелёное" if num == 0 else ("красное" if num % 2 == 1 else "чёрное")
        vip_ok, _ = is_vip_active(uid_int)
        if drop == chosen:
            win = bet * (14 if chosen == "зелёное" else 2) * (2 if vip_ok else 1)
            bal = record_game_result(uid_int, win, True)
            return await message.answer(f"🎡 Выпало `{num}` (**{drop}**)! Победа `+{format_coins(win)}`! Баланс: `{format_coins(bal)}`.")
        bal = record_game_result(uid_int, -bet, False)
        return await message.answer(f"🎡 Выпало `{num}` (**{drop}**). Проигрыш `-{format_coins(bet)}`. Баланс: `{format_coins(bal)}`.")

    elif cmd in ("дуэль", "duel"):
        tid, rem = await resolve_target(message, params)
        if not tid or not rem or not rem[0].isdigit():
            return await message.answer("❌ Формат: `/дуэль @ник [ставка]`")
        bet = int(rem[0])
        if int(tid) == uid_int or int(tid) <= 0 or bet <= 0:
            return await message.answer("❌ Укажи другого участника и ставку > 0.")
        if int(get_econ(uid_int)["coins"]) < bet or int(get_econ(int(tid))["coins"]) < bet:
            return await message.answer("❌ Недостаточно монет у одного из игроков.")
        token_key = f"{uid_int}_{int(tid)}_{int(time.time()) % 100000}"
        db["pending_duels"][token_key] = {"challenger_id": uid_int, "target_id": int(tid), "bet": bet, "peer_id": peer_id, "created_at": int(time.time())}
        save_db(db)
        return await message.answer(
            f"⚔️ {await format_vk_user(uid_int)} вызывает {await format_vk_user(int(tid))} на дуэль на `{format_coins(bet)}` монет!",
            keyboard=get_duel_keyboard(token_key),
        )

    elif cmd == "дуэль_принять" and params:
        duel = db.get("pending_duels", {}).get(params[0])
        if not duel:
            return await message.answer("⚠️ Дуэль не найдена.")
        if uid_int != int(duel["target_id"]) and not is_main_owner:
            return await message.answer("❌ Принять дуэль может только вызванный игрок.")
        db["pending_duels"].pop(params[0], None)
        save_db(db)
        c_id, t_id, bet = int(duel["challenger_id"]), int(duel["target_id"]), int(duel["bet"])
        w_id, l_id = random.choice([(c_id, t_id), (t_id, c_id)])
        w_bal = record_game_result(w_id, bet, True)
        l_bal = record_game_result(l_id, -bet, False)
        return await message.answer(
            f"🏆 Победил {await format_vk_user(w_id)} (`+{format_coins(bet)}` монет)! Баланс: `{format_coins(w_bal)}` | Проигравший: `{format_coins(l_bal)}`."
        )

    elif cmd == "дуэль_отмена" and params:
        duel = db.get("pending_duels", {}).get(params[0])
        if duel and uid_int in (int(duel["challenger_id"]), int(duel["target_id"]), MAIN_OWNER_ID):
            db["pending_duels"].pop(params[0], None)
            save_db(db)
            return await message.answer("❌ Дуэль отменена.")

    elif cmd in ("выдатьмонеты", "givecoins"):
        if not is_main_owner:
            return await message.answer("❌ Выдавать монеты может только владелец бота.")
        if message.reply_message and params and params[0].isdigit():
            tid, amt = int(message.reply_message.from_id), int(params[0])
        else:
            t_id, rem = await resolve_target(message, params)
            if not t_id or not rem or not rem[0].isdigit():
                return await message.answer("❌ Формат: `/выдатьмонеты @ник 10000000`")
            tid, amt = int(t_id), int(rem[0])
        econ = get_econ(tid)
        with DB_LOCK:
            econ["coins"] = int(econ.get("coins", 0)) + amt
            save_db(db)
        return await message.answer(f"💰 Выдано `+{format_coins(amt)}` монет {await format_vk_user(tid)}. Баланс: `{format_coins(econ['coins'])}`.")

    elif cmd in ("снятьмонеты", "takecoins"):
        if not is_main_owner:
            return await message.answer("❌ Снимать монеты может только владелец бота.")
        t_id, rem = await resolve_target(message, params)
        if not t_id or not rem or not rem[0].isdigit():
            return await message.answer("❌ Формат: `/снятьмонеты @ник 10000000`")
        amt = int(rem[0])
        econ = get_econ(int(t_id))
        with DB_LOCK:
            econ["coins"] = max(0, int(econ.get("coins", 0)) - amt)
            save_db(db)
        return await message.answer(f"📉 Списано `{format_coins(amt)}` монет у {await format_vk_user(int(t_id))}. Баланс: `{format_coins(econ['coins'])}`.")

    elif cmd in ("вип", "vip", "выдатьвип"):
        if not is_main_owner:
            return await message.answer("❌ Выдавать VIP-статус может только владелец бота.")
        if message.reply_message and params:
            tid, dur_raw = int(message.reply_message.from_id), params[0]
        else:
            t_id, rem = await resolve_target(message, params)
            if not t_id or not rem:
                return await message.answer("❌ Формат: `/вип @ник 30д` (от `1д` до `10лет` / `3650д`)")
            tid, dur_raw = int(t_id), rem[0]
        sec = parse_vip_seconds(dur_raw)
        if not sec:
            return await message.answer("❌ Срок VIP от 1 дня до 10 лет (например: `1д`, `30д`, `1год`, `10лет` или `1..3650`).")
        econ = get_econ(tid)
        now_s = int(time.time())
        new_until = max(now_s, int(econ.get("vip_until", 0))) + sec
        with DB_LOCK:
            econ["vip_until"] = new_until
            save_db(db)
        return await message.answer(
            f"👑 Выдан **VIP-статус** {await format_vk_user(tid)} на `{format_duration_ru(sec)}` (до `{time.strftime('%d.%m.%Y %H:%M', time.localtime(new_until))}`)."
        )

    elif cmd in ("снятьвип", "delvip"):
        if not is_main_owner:
            return await message.answer("❌ Снимать VIP-статус может только владелец бота.")
        t_id, _ = await resolve_target(message, params)
        if not t_id:
            return await message.answer("❌ Формат: `/снятьвип @ник`")
        econ = get_econ(int(t_id))
        with DB_LOCK:
            econ["vip_until"] = 0
            save_db(db)
        return await message.answer(f"✅ VIP-статус у {await format_vk_user(int(t_id))} снят.")

    # =================================================================
    # ПРОВЕРКА ПУЛА ДЛЯ /г... И /g...
    # =================================================================
    if cmd.startswith("г") or cmd.startswith("g"):
        if not has_global_pool(chat_data) and not is_main_owner:
            return await message.answer(
                f"🔒 **Команда `/{cmd}` относится к пулу `/г...` и `/g...`!**\n"
                "Активировать бесконечный ключ (`1 код = ∞ чатов`): `/код [КЛЮЧ]`.",
                keyboard=get_chat_inline_keyboard(),
            )

    # =================================================================
    # КОМАНДЫ АКТИВАЦИИ И СОЗДАНИЯ КОДОВ ПУЛА
    # =================================================================
    if cmd in ["код", "code", "активировать", "activate"]:
        if not params:
            return await message.answer("❌ Формат: `/код [КЛЮЧ]`")
        _, reply_msg = activate_pool_code_for_chat(peer_id, params[0])
        return await message.answer(reply_msg, keyboard=get_chat_inline_keyboard())

    elif cmd in ["создатькод", "выдатькод", "genrand", "genpool"]:
        if not is_main_owner:
            return await message.answer("❌ Генерация кодов доступна только владельцу бота.")
        p_type = params[0].lower() if params else "all"
        if p_type not in ("global", "nick_roles", "all"):
            return await message.answer("❌ Формат: `/создатькод [global|nick_roles|all]`")
        code = generate_random_code(p_type, uid_int)
        return await message.answer(
            f"🎲 **Бесконечный код создан (`∞` бесед):** `{code}` (`{p_type}`)\n• Активация в беседе: `/код {code}`"
        )

    elif cmd in ["коды", "codes"]:
        if not is_main_owner:
            return await message.answer("❌ Просмотр кодов доступен только владельцу бота.")
        codes = db.get("pool_codes", {})
        if not codes:
            return await message.answer("📋 Выданных кодов пока нет.")
        lines = [f"• `{c_key}` | `{c_info.get('type')}` | чатов: `{len(c_info.get('chats', []))}` (`∞`)" for c_key, c_info in list(codes.items())[-25:]]
        return await message.answer("📋 **БЕСКОНЕЧНЫЕ КОДЫ ПУЛА:**\n\n" + "\n".join(lines))

    elif cmd in ["отозватькод", "удалитькод", "revokecode"]:
        if not is_main_owner:
            return await message.answer("❌ Отзыв кодов доступен только владельцу бота.")
        if not params:
            return await message.answer("❌ Формат: `/отозватькод [КЛЮЧ]`")
        _, msg_rev = revoke_pool_code(params[0])
        return await message.answer(msg_rev)

    elif cmd in ["пулники", "nickpool"]:
        if not is_main_owner and user_lvl < 95:
            return await message.answer("❌ Требуется владелец бота или 95+ lvl.")
        chat_data["pools"]["nick_roles"] = not bool(chat_data["pools"].get("nick_roles"))
        save_db(db)
        st = "🟢 ВКЛЮЧЁН" if chat_data["pools"]["nick_roles"] else "🔴 ВЫКЛЮЧЕН"
        return await message.answer(f"🎭 Отдельный пул «Ники и роли» в этой беседе: {st}.")

    # =================================================================
    # УТИЛИТЫ, РЕПУТАЦИЯ, ЗАМЕТКИ, СОЗЫВ, ВЫДАЧА ВЛАДЕЛЬЦА
    # =================================================================
    elif cmd in ["мойид", "myid", "ид", "id"]:
        return await message.answer(f"🆔 Ваш VK ID: `{uid_int}` | Peer ID беседы: `{peer_id}`")

    elif cmd in ["время", "time", "дата", "date"]:
        return await message.answer(f"🕒 Текущее время сервера: `{time.strftime('%Y-%m-%d %H:%M:%S')}`")

    elif cmd in ["реп", "rep"]:
        tid, _ = await resolve_target(message, params)
        if not tid or tid <= 0:
            return await message.answer("❌ Укажите участника (`@ник`, `id` или ответом на сообщение): `/реп @ник`")
        if tid == uid_int:
            return await message.answer("❌ Самому себе репутацию выдать нельзя.")
        cd_key = (peer_id, uid_int)
        now_t = time.time()
        if now_t - rep_cooldown.get(cd_key, 0) < 3600 and not is_main_owner:
            mins_left = int((3600 - (now_t - rep_cooldown.get(cd_key, 0))) // 60) + 1
            return await message.answer(f"⏳ Вы уже выдавали репутацию. Повторно через `{mins_left}` мин.")
        rep_cooldown[cd_key] = now_t
        t_str = str(tid)
        chat_data["reputation"][t_str] = int(chat_data["reputation"].get(t_str, 0)) + 1
        save_db(db)
        return await message.answer(f"⭐ {await format_vk_user(uid_int)} повысил репутацию {await format_vk_user(tid)}! Репутация: **`{chat_data['reputation'][t_str]}`**")

    elif cmd in ["топреп", "toprep"]:
        reps = chat_data.get("reputation", {})
        if not reps:
            return await message.answer("⭐ В этой беседе пока ни у кого нет репутации.")
        sorted_rep = sorted(reps.items(), key=lambda x: int(x[1]), reverse=True)[:10]
        lines = [f"{idx + 1}. {await format_vk_user(int(u_id))} — **`{val}`** ⭐" for idx, (u_id, val) in enumerate(sorted_rep)]
        return await message.answer("🏆 ═══ ТОП РЕПУТАЦИИ БЕСЕДЫ ═══ 🏆\n\n" + "\n".join(lines))

    elif cmd in ["созыв", "call", "all"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        call_msg = " ".join(params).strip() if params else "Внимание всем участникам беседы!"
        return await message.answer(f"📣 **ОБЩИЙ СОЗЫВ АДМИНИСТРАЦИИ**\n• Инициатор: {await format_vk_user(uid_int)}\n• Сообщение: **{call_msg}**\n\n@all")

    elif cmd in ["кто", "who"]:
        if not params:
            return await message.answer("❌ Формат: `/кто [текст/вопрос]`")
        try:
            members_res = await bot.api.messages.get_conversation_members(peer_id=peer_id)
            pool_ids = [int(m.member_id) for m in (members_res.items or []) if int(m.member_id) > 0]
        except Exception:
            pool_ids = [int(k) for k in chat_data.get("members", {}).keys() if k.lstrip("-").isdigit() and int(k) > 0]
        if not pool_ids:
            pool_ids = [uid_int]
        return await message.answer(f"🎲 Я думаю, что {' '.join(params)} — это {await format_vk_user(secrets.choice(pool_ids))}!")

    elif cmd in ["шанс", "chance"]:
        return await message.answer(f"📊 Шанс того, что {' '.join(params) if params else 'этого события'} — **`{secrets.randbelow(101)}%`**")

    elif cmd in ["выбор", "choose"]:
        raw_q = raw_text.split(None, 1)[1] if len(raw_text.split(None, 1)) > 1 else ""
        opts = [o.strip() for o in raw_q.replace("|", "/").split("/") if o.strip()]
        if len(opts) < 2:
            return await message.answer("❌ Формат: `/выбор вариант 1 / вариант 2`")
        return await message.answer(f"🎯 Выбираю: **{secrets.choice(opts)}**")

    elif cmd in ["заметки", "notes"]:
        notes = chat_data.get("notes", {})
        if not notes:
            return await message.answer("📝 В этой беседе пока нет заметок.")
        return await message.answer("📝 ═══ ЗАМЕТКИ БЕСЕДЫ ═══ 📝\n\n" + "\n".join([f"• **{k}**: {v}" for k, v in notes.items()]))

    elif cmd in ["заметка", "addnote"]:
        if user_lvl < 15:
            return await message.answer(f"❌ Требуется 15+ уровень (у вас: {user_lvl}).")
        body = raw_text.split(None, 1)[1].strip() if len(raw_text.split(None, 1)) > 1 else ""
        if "|" not in body:
            return await message.answer("❌ Формат: `/заметка [название] | [текст]`")
        k, v = [part.strip() for part in body.split("|", 1)]
        chat_data["notes"][k.lower()] = v
        save_db(db)
        return await message.answer(f"✅ Заметка **{k.lower()}** сохранена.")

    elif cmd in ["удзаметку", "delnote"]:
        if user_lvl < 15:
            return await message.answer(f"❌ Требуется 15+ уровень (у вас: {user_lvl}).")
        k = " ".join(params).strip().lower()
        if k in chat_data.get("notes", {}):
            del chat_data["notes"][k]
            save_db(db)
            return await message.answer(f"🗑 Заметка **{k}** удалена.")
        return await message.answer("❌ Заметка не найдена.")

    elif cmd in ["явладелец", "claimowner"]:
        if chat_data.get("owner_id") and not is_main_owner:
            return await message.answer(f"❌ Владелец в этой беседе уже закреплён: {await format_vk_user(int(chat_data['owner_id']))}.")
        is_vk_owner, is_vk_admin = await get_vk_chat_member_flags(peer_id, uid_int)
        if not (is_vk_owner or is_vk_admin or is_main_owner):
            return await message.answer("⛔ Команда `/явладелец` доступна только создателю или админу беседы ВК.")
        sync_owner_member(chat_data, int(uid_int))
        save_db(db)
        return await message.answer("👑 Статус подтверждён! Вы закреплены владельцем этой беседы (`100 lvl`).")

    elif cmd in ["сбросвладельца", "resetowner"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        old_owner = chat_data.get("owner_id")
        chat_data["owner_id"] = None
        if old_owner and str(old_owner) in chat_data["members"]:
            del chat_data["members"][str(old_owner)]
        save_db(db)
        return await message.answer("♻️ Владелец беседы сброшен.")

    elif cmd in ["setrules", "сетправила"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        new_rules = raw_text.split(None, 1)[1].strip() if len(raw_text.split(None, 1)) > 1 else ""
        if not new_rules:
            return await message.answer("❌ Формат: `/сетправила [текст]`")
        chat_data["rules_text"] = new_rules
        save_db(db)
        return await message.answer("✅ Новые правила беседы сохранены!")

    elif cmd in ["resetrules", "сбросправил"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["rules_text"] = DEFAULT_RULES
        save_db(db)
        return await message.answer("♻️ Правила беседы сброшены к стандартным.")

    elif cmd in ["сетпривет", "setgreet"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        g_txt = raw_text.split(None, 1)[1].strip() if len(raw_text.split(None, 1)) > 1 else ""
        if not g_txt:
            return await message.answer("❌ Формат: `/сетпривет Добро пожаловать, {user}, в {chat}!`")
        chat_data["greeting_text"] = g_txt
        save_db(db)
        return await message.answer("✅ Приветствие при входе сохранено.")

    elif cmd in ["сброспривета", "resetgreet"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["greeting_text"] = ""
        save_db(db)
        return await message.answer("♻️ Приветствие при входе отключено.")

    elif cmd in ["setproject", "сетназвание"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        new_name = raw_text.split(None, 1)[1].strip() if len(raw_text.split(None, 1)) > 1 else ""
        if not new_name:
            return await message.answer("❌ Формат: `/сетназвание [Название]`")
        chat_data["project_name"] = new_name[:50]
        save_db(db)
        return await message.answer(f"✅ Название проекта изменено на: **{chat_data['project_name']}**")

    elif cmd in ["seturl", "сетссылка"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        if not params:
            return await message.answer("❌ Формат: `/сетссылка [URL]`")
        chat_data["project_url"] = params[0].strip()
        save_db(db)
        return await message.answer(f"✅ Ссылка проекта обновлена: {chat_data['project_url']}")

    elif cmd in ["ping", "пинг"]:
        t0 = time.time()
        await message.answer("🏓 Измеряем задержку...")
        ping_ms = int((time.time() - t0) * 1000)
        return await message.answer(f"🏓 **PONG!** Отклик API VK: `{ping_ms} ms`")

    elif cmd in ["status", "статус"]:
        uptime_sec = int(time.time() - START_TIME)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        return await message.answer(
            f"⚡ ═══ СТАТУС ЯДРА {proj_name} ═══ ⚡\n\n"
            f"• Аптайм: `{hours}ч {minutes}м {seconds}с`\n"
            f"• Подключено чатов: `{len(db.get('global_chats', []))}`\n"
            f"• Пул `/г...`: `{'Активен 🟢' if has_global_pool(chat_data) else 'Не активен 🔴'}`\n"
            f"• Пул Ники/Роли: `{'Активен 🟢' if has_nick_roles_pool(chat_data) else 'Не активен 🔴'}`"
        )

    elif cmd in ["info", "инфо"]:
        owner_fmt = await format_vk_user(int(owner_id)) if owner_id else "Не определён"
        return await message.answer(
            f"ℹ️ ═══ ИНФОРМАЦИЯ О БЕСЕДЕ ═══ ℹ️\n\n"
            f"• Беседа: **{chat_title}**\n"
            f"• Проект: **{proj_name}**\n"
            f"• Peer ID: `{peer_id}`\n"
            f"• Владелец: {owner_fmt}\n"
            f"• Ваш уровень: `{user_lvl} lvl` ({get_role_title(user_lvl, chat_data, is_chat_owner(uid_int, chat_data, owner_id))})"
        )

    elif cmd in ["report", "репорт"]:
        if not params:
            return await message.answer("❌ Формат: `/репорт [текст]`")
        rep_text = " ".join(params).strip()
        rep_id = len(db["reports"]) + 1
        db["reports"].append({
            "id": rep_id,
            "peer_id": peer_id,
            "chat_title": chat_title,
            "from_id": uid_int,
            "text": rep_text,
            "status": "open",
            "reply": None,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        save_db(db)
        if owner_id and int(owner_id) != uid_int:
            try:
                await bot.api.messages.send(
                    peer_id=int(owner_id),
                    message=f"📩 **РЕПОРТ #{rep_id}** в **{chat_title}** от {await format_vk_user(uid_int)}:\n{rep_text}",
                    random_id=random.randint(1, 2_000_000_000),
                )
            except Exception:
                pass
        return await message.answer(f"✅ Репорт **#{rep_id}** отправлен!")

    elif cmd in ["reports", "репорты"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        chat_reps = [r for r in db.get("reports", []) if r.get("peer_id") == peer_id][-8:]
        if not chat_reps:
            return await message.answer("📋 Репортов пока нет.")
        lines = [f"• **#{r['id']}** [{'🟢' if r.get('status')=='closed' else '🟡'}] от {await format_vk_user(r['from_id'])}: {r['text']}" for r in chat_reps]
        return await message.answer("📩 ═══ РЕПОРТЫ БЕСЕДЫ ═══ 📩\n\n" + "\n".join(lines))

    elif cmd in ["ответить", "answer"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        if len(params) < 2 or not params[0].lstrip("#").isdigit():
            return await message.answer("❌ Формат: `/ответить [№] [ответ]`")
        rep_num = int(params[0].lstrip("#"))
        ans_txt = " ".join(params[1:]).strip()
        found = next((r for r in db.get("reports", []) if r.get("id") == rep_num), None)
        if not found:
            return await message.answer(f"❌ Репорт #{rep_num} не найден.")
        found["reply"] = ans_txt
        found["status"] = "closed"
        save_db(db)
        return await message.answer(f"✉️ Ответ по репорту **#{rep_num}** для {await format_vk_user(found['from_id'])}: **{ans_txt}**")

    # =================================================================
    # ПУЛ «НИКИ И РОЛИ»
    # =================================================================
    elif cmd in ["ник", "nick"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Команда `/ник` входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if not params:
            return await message.answer("❌ Формат: `/ник [Никнейм]`")
        new_nick = " ".join(params).strip()[:40]
        cur_lvl = get_user_level_by_id(uid_int, chat_data, owner_id)
        if uid_str not in chat_data["members"] or not isinstance(chat_data["members"][uid_str], dict):
            chat_data["members"][uid_str] = {"level": cur_lvl, "nickname": new_nick}
        else:
            chat_data["members"][uid_str]["nickname"] = new_nick
        save_db(db)
        return await message.answer(f"✅ Ваш ник обновлён на: **{new_nick}**")

    elif cmd in ["nlist", "нлист"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Список ников входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        lines = [
            f"• {await format_vk_user(int(m_id))} ➔ **{info['nickname']}**"
            for m_id, info in chat_data.get("members", {}).items()
            if isinstance(info, dict) and info.get("nickname", "").strip()
        ]
        return await message.answer("📋 ═══ АКТИВНЫЕ НИКНЕЙМЫ ═══ 📋\n\n" + "\n".join(lines) if lines else "📋 Ников пока нет.")

    elif cmd in ["snick", "сник", "setnick", "сетник"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Выдача ников входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        orig_parts = raw_text.split()[1:]
        tid, remaining = await resolve_target(message, orig_parts)
        if not tid:
            return await message.answer("❌ Пользователь не найден.")
        new_nick = " ".join(orig_parts).strip() if message.reply_message else " ".join(remaining).strip()
        if not new_nick:
            return await message.answer("❌ Укажите ник.")
        t_str = str(tid)
        cur_lvl = get_user_level_by_id(tid, chat_data, owner_id)
        if t_str not in chat_data["members"] or not isinstance(chat_data["members"][t_str], dict):
            chat_data["members"][t_str] = {"level": cur_lvl, "nickname": new_nick}
        else:
            chat_data["members"][t_str]["nickname"] = new_nick
        save_db(db)
        return await message.answer(f"✅ Ник **{new_nick}** выдан {await format_vk_user(tid)}.")

    elif cmd in ["delnick", "делник"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Удаление ников входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in chat_data["members"] and isinstance(chat_data["members"][str(tid)], dict):
            chat_data["members"][str(tid)]["nickname"] = ""
            save_db(db)
            return await message.answer(f"🟢 Ник удалён у {await format_vk_user(tid)}.")
        return await message.answer("❌ Ник не найден.")

    elif cmd in ["newrole", "новаяроль"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Создание ролей входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        if len(params) < 2 or not params[0].isdigit():
            return await message.answer("❌ Формат: `/новаяроль [ур] [Название]`")
        chat_data["role_names"][str(int(params[0]))] = " ".join(params[1:])
        save_db(db)
        return await message.answer(f"✅ Роль `{int(params[0])} lvl` сохранена: **{' '.join(params[1:])}**")

    elif cmd in ["delrole", "удалитьроль"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Удаление ролей входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        if params and params[0].isdigit() and str(int(params[0])) in chat_data["role_names"]:
            del chat_data["role_names"][str(int(params[0]))]
            save_db(db)
            return await message.answer(f"🗑 Роль `{int(params[0])}` удалена.")
        return await message.answer("❌ Роль не найдена.")

    elif cmd in ["resetroles", "сбросролей"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Сброс ролей входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        chat_data["role_names"] = {str(k): v for k, v in DEFAULT_ROLES.items()}
        save_db(db)
        return await message.answer("♻️ Роли сброшены к стандартным.")

    elif cmd in ["setadmin", "сетадмин", "выдатьадм", "выдатьроль", "роль"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Выдача ролей входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        lvl_val = next((int(p) for p in rem if p.isdigit()), None)
        if not tid or lvl_val is None:
            return await message.answer("❌ Формат: `/сетадмин @ник [уровень]`")
        t_str = str(tid)
        cur_nick = chat_data["members"].get(t_str, {}).get("nickname", "") if isinstance(chat_data["members"].get(t_str), dict) else ""
        chat_data["members"][t_str] = {"level": lvl_val, "nickname": cur_nick}
        save_db(db)
        return await message.answer(f"✅ {await format_vk_user(tid)} присвоен ранг `{lvl_val} lvl` (**{get_role_title(lvl_val, chat_data)}**).")

    elif cmd in ["повысить", "promote"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Повышение входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        new_lvl = int(rem[0]) if rem and rem[0].isdigit() else min(95, get_user_level_by_id(tid, chat_data, owner_id) + 15)
        t_str = str(tid)
        cur_nick = chat_data["members"].get(t_str, {}).get("nickname", "") if isinstance(chat_data["members"].get(t_str), dict) else ""
        chat_data["members"][t_str] = {"level": new_lvl, "nickname": cur_nick}
        save_db(db)
        return await message.answer(f"⬆️ {await format_vk_user(tid)} повышен до `{new_lvl} lvl`.")

    elif cmd in ["понизить", "demote"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Понижение входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        new_lvl = int(rem[0]) if rem and rem[0].isdigit() else max(0, get_user_level_by_id(tid, chat_data, owner_id) - 15)
        t_str = str(tid)
        cur_nick = chat_data["members"].get(t_str, {}).get("nickname", "") if isinstance(chat_data["members"].get(t_str), dict) else ""
        chat_data["members"][t_str] = {"level": new_lvl, "nickname": cur_nick}
        save_db(db)
        return await message.answer(f"⬇️ {await format_vk_user(tid)} понижен до `{new_lvl} lvl`.")

    elif cmd in ["removerole", "снятьроль", "снятадм"]:
        if not has_nick_roles_pool(chat_data) and not is_main_owner:
            return await message.answer("🔒 Снятие ролей входит в пул «Ники и роли». Активируйте ключ `/код [КЛЮЧ]`.")
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        t_str = str(tid)
        cur_nick = chat_data["members"].get(t_str, {}).get("nickname", "") if isinstance(chat_data["members"].get(t_str), dict) else ""
        chat_data["members"][t_str] = {"level": 0, "nickname": cur_nick}
        save_db(db)
        return await message.answer(f"🟢 С {await format_vk_user(tid)} сняты права админки (0 lvl).")

    # =================================================================
    # ВЫГОВОРЫ, СТАТА, БАН С КНОПКАМИ (1-360 ДНЕЙ) И МОДЕРАЦИЯ
    # =================================================================
    elif cmd in ["выговор", "reprimand", "vig"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/выговор @ник [причина]`")
        reason = " ".join(rem) if rem else "Нарушение регламента"
        t_str = str(tid)
        chat_data["reprimands"].setdefault(t_str, []).append(reason)
        cnt = len(chat_data["reprimands"][t_str])
        if cnt >= 3:
            cur_nick = chat_data["members"].get(t_str, {}).get("nickname", "") if isinstance(chat_data["members"].get(t_str), dict) else ""
            chat_data["members"][t_str] = {"level": 0, "nickname": cur_nick}
            chat_data["reprimands"][t_str] = []
            save_db(db)
            return await message.answer(f"🚨 {await format_vk_user(tid)} получил `3/3` выговора и снят с должности!")
        save_db(db)
        return await message.answer(f"📕 {await format_vk_user(tid)} получил выговор (`{cnt}/3`). Причина: {reason}")

    elif cmd in ["выговоры", "reprimands", "vigs"]:
        tid, _ = await resolve_target(message, params)
        tid = tid or uid_int
        r_list = chat_data.get("reprimands", {}).get(str(tid), [])
        return await message.answer(f"📕 Выговоры {await format_vk_user(tid)} (`{len(r_list)}/3`):\n" + ("\n".join([f"{i+1}. {r}" for i, r in enumerate(r_list)]) if r_list else "Нет"))

    elif cmd in ["снятьвыговор", "unreprimand", "unvig"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and chat_data.get("reprimands", {}).get(str(tid)):
            chat_data["reprimands"][str(tid)].pop()
            save_db(db)
            return await message.answer(f"🟢 Последний выговор снят у {await format_vk_user(tid)}.")
        return await message.answer("❌ Выговоров нет.")

    elif cmd in ["снятьвсевыговоры", "clearreprimands"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        chat_data["reprimands"][str(tid)] = []
        save_db(db)
        return await message.answer(f"🟢 Все выговоры сняты у {await format_vk_user(tid)}.")

    elif cmd in ["stats", "стата", "профиль"]:
        tid, _ = await resolve_target(message, params)
        tid = int(tid) if tid else uid_int
        t_str = str(tid)
        owner_flag = is_chat_owner(tid, chat_data, owner_id)
        target_lvl = 100 if owner_flag else get_user_level_by_id(tid, chat_data, owner_id)
        r_name = get_role_title(target_lvl, chat_data, owner_flag=owner_flag)
        m_info = chat_data["members"].get(t_str, {})
        nick = m_info.get("nickname", "Не установлен") if isinstance(m_info, dict) and m_info.get("nickname") else "Не установлен"
        econ = get_econ(tid)
        vip_ok, vip_left = is_vip_active(tid)
        vip_str = f"👑 Активен ({format_duration_ru(vip_left)})" if vip_ok else "Нет"
        warns = len(chat_data.get("warns", {}).get(t_str, []))
        vigs = len(chat_data.get("reprimands", {}).get(t_str, []))
        rep_pts = int(chat_data.get("reputation", {}).get(t_str, 0))
        return await message.answer(
            f"📊 ═══ ДОСЬЕ ИГРОКА ═══ 📊\n"
            f"👤 {await format_vk_user(tid)}{' 👑' if owner_flag else ''}\n\n"
            f"• Ник в чате: `{nick}`\n"
            f"• Должность: **{r_name}** `[{target_lvl} lvl]`\n"
            f"• Баланс: `{format_coins(econ['coins'])}` монет\n"
            f"• VIP-статус: {vip_str}\n"
            f"• Репутация: `{rep_pts}` ⭐ | Варны: `{warns}/3` | Выговоры: `{vigs}/3`"
        )

    elif cmd in ["check", "проверка"]:
        tid, _ = await resolve_target(message, params)
        tid = int(tid) if tid else uid_int
        t_str = str(tid)
        owner_flag = is_chat_owner(tid, chat_data, owner_id)
        target_lvl = 100 if owner_flag else get_user_level_by_id(tid, chat_data, owner_id)
        r_name = get_role_title(target_lvl, chat_data, owner_flag=owner_flag)
        econ = get_econ(tid)
        vip_ok, vip_left = is_vip_active(tid)
        return await message.answer(
            f"🔎 ═══ ПРОВЕРКА ═══ 🔎\n"
            f"👤 {await format_vk_user(tid)}\n"
            f"• Ранг: `{target_lvl} lvl` — **{r_name}**\n"
            f"• Монеты: `{format_coins(econ['coins'])}` | VIP: `{format_duration_ru(vip_left) if vip_ok else 'Нет'}`\n"
            f"• Варны: `{len(chat_data.get('warns', {}).get(t_str, []))}/3` | Выговоры: `{len(chat_data.get('reprimands', {}).get(t_str, []))}/3`"
        )

    elif cmd in ["lock", "лок"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        chat_data["locked"] = True
        save_db(db)
        return await message.answer("🔒 Беседа закрыта для участников `<45 lvl`.")

    elif cmd in ["unlock", "анлок"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        chat_data["locked"] = False
        save_db(db)
        return await message.answer("🔓 Беседа открыта.")

    elif cmd in ["mute", "мут"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите цель для мута.")
        mins = int(rem[0]) if rem and rem[0].isdigit() else 10
        chat_data["mutes"][str(tid)] = time.time() + (mins * 60)
        save_db(db)
        return await message.answer(f"🤫 {await format_vk_user(tid)} замучен на `{mins}` мин.")

    elif cmd in ["unmuteall", "размутвсех"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        cnt = len(chat_data.get("mutes", {}))
        chat_data["mutes"] = {}
        save_db(db)
        return await message.answer(f"🔊 Сняты все муты (`{cnt}`).")

    elif cmd in ["unmute", "unmite", "анмут", "размут"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in chat_data["mutes"]:
            del chat_data["mutes"][str(tid)]
            save_db(db)
            return await message.answer(f"🔊 Мут снят с {await format_vk_user(tid)}.")
        return await message.answer("❌ Активных мутов нет.")

    # --- БАН С КНОПКАМИ (1–360 ДНЕЙ) ---
    elif cmd in ["ban", "бан"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите цель: `/бан @ник` или ответом на сообщение `/бан`.")
        target_id = int(tid)
        if is_bot_owner(target_id) or (owner_id and target_id == int(owner_id)):
            return await message.answer("❌ Владельца заблокировать нельзя.")

        direct_days: Optional[int] = None
        for tok in rem:
            if tok.isdigit() and 1 <= int(tok) <= 360:
                direct_days = int(tok)
                break

        if direct_days is not None:
            until_ts = int(time.time()) + direct_days * 86400
            chat_data["bans"][str(target_id)] = {"until": until_ts, "reason": "Бан", "by": uid_int}
            save_db(db)
            if chat_id_num:
                try:
                    await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=target_id)
                except Exception:
                    pass
            return await message.answer(f"🚫 {await format_vk_user(target_id)} заблокирован на `{direct_days}` дн.")

        token_key = f"{uid_int}_{target_id}_{int(time.time()) % 100000}"
        db["pending_bans"][token_key] = {
            "admin_id": uid_int,
            "target_id": target_id,
            "peer_id": peer_id,
            "created_at": int(time.time()),
        }
        save_db(db)
        return await message.answer(
            f"🔒 Выбери срок блокировки для {await format_vk_user(target_id)} (`1–360` дней):",
            keyboard=get_ban_days_keyboard(token_key),
        )

    elif cmd == "бан_срок" and len(params) >= 2:
        if user_lvl < 45:
            return
        token_key = params[0]
        if not params[1].isdigit():
            return
        days = int(params[1])
        if not (1 <= days <= 360):
            return await message.answer("❌ Срок бана от 1 до 360 дней.")
        pending = db.get("pending_bans", {}).pop(token_key, None)
        if not pending:
            return await message.answer("⚠️ Запрос блокировки устарел, вызови `/бан` заново.")
        target_id = int(pending["target_id"])
        until_ts = int(time.time()) + days * 86400
        chat_data["bans"][str(target_id)] = {"until": until_ts, "reason": f"Бан {days} дн.", "by": uid_int}
        save_db(db)
        if chat_id_num:
            try:
                await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=target_id)
            except Exception:
                pass
        add_log(f"Бан {target_id} на {days} дн. в чате {peer_id} от {uid_int}")
        return await message.answer(
            f"🚫 {await format_vk_user(target_id)} заблокирован на `{days}` дн. "
            f"(до `{time.strftime('%d.%m.%Y %H:%M', time.localtime(until_ts))}`).",
        )

    elif cmd == "бан_отмена" and params:
        db.get("pending_bans", {}).pop(params[0], None)
        save_db(db)
        return await message.answer("✅ Блокировка отменена.")

    elif cmd in ["unban", "анбан", "разбан"]:
        if user_lvl < 55:
            return await message.answer(f"❌ Требуется 55+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in chat_data["bans"]:
            del chat_data["bans"][str(tid)]
            save_db(db)
            return await message.answer(f"🟢 {await format_vk_user(tid)} разбанен.")
        return await message.answer("❌ Игрока нет в бан-листе.")

    elif cmd in ["banlist", "банлист"]:
        bans = chat_data.get("bans", {})
        if not bans:
            return await message.answer("📋 Чёрный список пуст.")
        return await message.answer("📋 ═══ ЧЁРНЫЙ СПИСОК ═══ 📋\n\n" + "\n".join([f"• {await format_vk_user(int(b_id))} ➔ {info.get('reason')}" for b_id, info in bans.items()]))

    elif cmd in ["chekban", "checkban", "чекбан"]:
        tid, _ = await resolve_target(message, params)
        tid = tid or uid_int
        is_b = str(tid) in chat_data.get("bans", {}) or str(tid) in db["global_bans"]
        return await message.answer(f"🔍 Статус {await format_vk_user(tid)}: {'В бане 🔴' if is_b else 'Чист 🟢'}")

    elif cmd in ["statschat", "статачата"]:
        return await message.answer(f"📈 Чатов: `{len(db.get('global_chats', []))}` | Участников в базе чата: `{len(chat_data.get('members', {}))}`")

    elif cmd in ["kick", "кик"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid or not chat_id_num:
            return await message.answer("❌ Укажите цель.")
        try:
            await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=tid)
            return await message.answer(f"✅ {await format_vk_user(tid)} исключён.")
        except Exception as e:
            return await message.answer(f"❌ Ошибка кика: {e}")

    elif cmd in ["warn", "варн"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите цель.")
        reason = " ".join(rem) if rem else "Нарушение"
        t_str = str(tid)
        chat_data["warns"].setdefault(t_str, []).append(reason)
        save_db(db)
        cnt = len(chat_data["warns"][t_str])
        await message.answer(f"⚠️ {await format_vk_user(tid)} получил варн (`{cnt}/3`). Причина: {reason}")
        if cnt >= 3:
            if chat_id_num:
                try:
                    await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=tid)
                except Exception:
                    pass
            chat_data["warns"][t_str] = []
            save_db(db)
            await message.answer(f"🚫 Авто-кик за `3/3` варнов: {await format_vk_user(tid)}.")

    elif cmd in ["warns", "варны"]:
        tid, _ = await resolve_target(message, params)
        tid = tid or uid_int
        w_list = chat_data.get("warns", {}).get(str(tid), [])
        return await message.answer(f"⚠️ Варны {await format_vk_user(tid)} (`{len(w_list)}/3`):\n" + ("\n".join([f"{i+1}. {r}" for i, r in enumerate(w_list)]) if w_list else "Нет"))

    elif cmd in ["unwarn", "анварн", "снятварн"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and chat_data.get("warns", {}).get(str(tid)):
            chat_data["warns"][str(tid)].pop()
            save_db(db)
            return await message.answer(f"🟢 Последний варн снят у {await format_vk_user(tid)}.")
        return await message.answer("❌ Варнов нет.")

    elif cmd in ["clearwarns", "снятьвсеварны"]:
        if user_lvl < 55:
            return await message.answer(f"❌ Требуется 55+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid:
            chat_data["warns"][str(tid)] = []
            save_db(db)
            return await message.answer(f"🟢 Все варны сняты у {await format_vk_user(tid)}.")

    elif cmd in ["antispam", "антиспам"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["antispam"] = not bool(chat_data.get("antispam"))
        save_db(db)
        return await message.answer(f"🛡 Антиспам: `{'🟢 ВКЛ' if chat_data['antispam'] else '🔴 ВЫКЛ'}`.")

    elif cmd in ["caps", "антикапс"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["caps_guard"] = not bool(chat_data.get("caps_guard"))
        save_db(db)
        return await message.answer(f"🔠 Антикапс: `{'🟢 ВКЛ' if chat_data['caps_guard'] else '🔴 ВЫКЛ'}`.")

    elif cmd in ["antiflood", "антифлуд"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["antiflood"] = not bool(chat_data.get("antiflood"))
        save_db(db)
        return await message.answer(f"🌊 Антифлуд: `{'🟢 ВКЛ' if chat_data['antiflood'] else '🔴 ВЫКЛ'}`.")

    elif cmd in ["antiads", "антиреклама"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["antiads"] = not bool(chat_data.get("antiads"))
        save_db(db)
        return await message.answer(f"🚫 Антиреклама: `{'🟢 ВКЛ' if chat_data['antiads'] else '🔴 ВЫКЛ'}`.")

    elif cmd in ["clear", "очистить", "чистка"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        count = max(1, min(int(params[0]) if params and params[0].isdigit() else 10, 100))
        cmid = message.conversation_message_id or 0
        ids_to_del = list(range(max(1, cmid - count), cmid + 1))
        try:
            await bot.api.messages.delete(peer_id=peer_id, conversation_message_ids=ids_to_del, delete_for_all=True)
            msg_done = await message.answer(f"🧹 Очищено до `{len(ids_to_del)}` сообщений.")
            await asyncio.sleep(3)
            try:
                await bot.api.messages.delete(peer_id=peer_id, conversation_message_ids=[msg_done.conversation_message_id], delete_for_all=True)
            except Exception:
                pass
        except Exception:
            return await message.answer("❌ Нужны права администратора беседы.")

    elif cmd in ["logs", "логи"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        recent = "\n".join(db["logs"][-15:])
        return await message.answer(f"📜 ═══ ЛОГИ ═══ 📜\n\n{recent if recent else 'Пусто'}")

    elif cmd in ["pin", "закреп"]:
        if user_lvl < 45 or not message.reply_message:
            return await message.answer("❌ Нужны права 45+ lvl и реплай на сообщение.")
        try:
            await bot.api.messages.pin(peer_id=peer_id, conversation_message_id=message.reply_message.conversation_message_id)
            return await message.answer("📌 Закреплено!")
        except Exception:
            return await message.answer("❌ Ошибка закрепления.")

    elif cmd in ["unpin", "откреп"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        try:
            await bot.api.messages.unpin(peer_id=peer_id)
            return await message.answer("📌 Откреплено.")
        except Exception:
            return await message.answer("❌ Ошибка открепления.")

    elif cmd in ["setowner", "сетвладелец"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid:
            sync_owner_member(chat_data, int(tid))
            save_db(db)
            return await message.answer(f"👑 Владельцем чата закреплён {await format_vk_user(tid)} (`100 lvl`).")

    elif cmd in ["setleader", "сетлидер", "поставитьлидера"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if tid and rem:
            db["leaders"][str(tid)] = " ".join(rem)
            save_db(db)
            return await message.answer(f"👔 {await format_vk_user(tid)} назначен лидером **{' '.join(rem)}**.")

    elif cmd in ["removeleader", "снятлидера"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in db["leaders"]:
            del db["leaders"][str(tid)]
            save_db(db)
            return await message.answer(f"🟢 {await format_vk_user(tid)} снят с лидерки.")

    elif cmd in ["leaders", "лидеры"]:
        if not db.get("leaders"):
            return await message.answer("👔 Лидеров пока нет.")
        return await message.answer("👔 ═══ ЛИДЕРЫ ═══ 👔\n\n" + "\n".join([f"• {await format_vk_user(int(l_id))} ➔ **{org}**" for l_id, org in db["leaders"].items()]))

    # =================================================================
    # СЕТЕВЫЕ КОМАНДЫ НА /г... И /g...
    # =================================================================
    elif cmd in ["grole", "гроль"]:
        if user_lvl < 95:
            return await message.answer(f"❌ Требуется 95+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if tid and rem and rem[0].isdigit():
            db["global_roles"][str(tid)] = int(rem[0])
            save_db(db)
            return await message.answer(f"🌐 Глобальная роль `{rem[0]}` выдана {await format_vk_user(tid)}.")

    elif cmd in ["grr", "снятьгроль"]:
        if user_lvl < 95:
            return await message.answer(f"❌ Требуется 95+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in db["global_roles"]:
            del db["global_roles"][str(tid)]
            save_db(db)
            return await message.answer(f"🟢 Глобальная роль снята у {await format_vk_user(tid)}.")

    elif cmd in ["gban", "гбан", "гбн"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if tid:
            reason = " ".join(rem) if rem else "Глобальное нарушение"
            db["global_bans"][str(tid)] = reason
            save_db(db)
            if chat_id_num:
                try:
                    await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=tid)
                except Exception:
                    pass
            return await message.answer(f"⛔ ГЛОБАЛЬНЫЙ БАН для {await format_vk_user(tid)}! Причина: {reason}")

    elif cmd in ["gunban", "гунбан", "гразбан"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in db["global_bans"]:
            del db["global_bans"][str(tid)]
            save_db(db)
            return await message.answer(f"🟢 {await format_vk_user(tid)} снят с глобального бана.")

    elif cmd in ["gbanlist", "гбанлист"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        gbans = db.get("global_bans", {})
        return await message.answer("⛔ ═══ ГЛОБАЛЬНЫЙ БАН-ЛИСТ ═══ ⛔\n\n" + "\n".join([f"• {await format_vk_user(int(b_id))} ➔ {rsn}" for b_id, rsn in list(gbans.items())[-25:]]) if gbans else "Пусто.")

    elif cmd in ["gcheckban", "гчекбан"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        tid = tid or uid_int
        rsn = db.get("global_bans", {}).get(str(tid))
        return await message.answer(f"🌍 {await format_vk_user(tid)}: " + (f"⛔ В глобальном бане ({rsn})" if rsn else "🟢 Чист"))

    elif cmd in ["гмут", "gmute"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if tid:
            mins = int(rem[0]) if rem and rem[0].isdigit() else 15
            for cid in db.get("global_chats", []):
                get_chat_data(int(cid))["mutes"][str(tid)] = time.time() + (mins * 60)
            save_db(db)
            return await message.answer(f"🌍🤫 {await format_vk_user(tid)} замучен на `{mins}` мин по сети.")

    elif cmd in ["гранмут", "gunmute"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid:
            for cid in db.get("global_chats", []):
                get_chat_data(int(cid)).get("mutes", {}).pop(str(tid), None)
            save_db(db)
            return await message.answer(f"🌍🔊 Сетевой мут снят с {await format_vk_user(tid)}.")

    elif cmd in ["гкик", "gkick"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid:
            kicked = 0
            for cid in db.get("global_chats", []):
                if int(cid) > 2000000000:
                    try:
                        await bot.api.messages.remove_chat_user(chat_id=int(cid) - 2000000000, user_id=tid)
                        kicked += 1
                    except Exception:
                        pass
            return await message.answer(f"🌍🥾 {await format_vk_user(tid)} исключён из `{kicked}` бесед.")

    elif cmd in ["гварн", "gwarn"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if tid:
            rsn = " ".join(rem) if rem else "Сетевое предупреждение"
            for cid in db.get("global_chats", []):
                get_chat_data(int(cid)).setdefault("warns", {}).setdefault(str(tid), []).append(f"[СЕТЬ] {rsn}")
            save_db(db)
            return await message.answer(f"🌍⚠️ {await format_vk_user(tid)} получил сетевой варн: {rsn}")

    elif cmd in ["гвыговор", "greprimand"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if tid:
            rsn = " ".join(rem) if rem else "Нарушение сетевого регламента"
            db["global_reprimands"].setdefault(str(tid), []).append(rsn)
            save_db(db)
            return await message.answer(f"🌍📕 Сетевой выговор выдан {await format_vk_user(tid)}: {rsn}")

    elif cmd in ["гвыговоры", "greprimands"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        tid = tid or uid_int
        r_list = db.get("global_reprimands", {}).get(str(tid), [])
        return await message.answer(f"🌍📕 Сетевые выговоры {await format_vk_user(tid)} (`{len(r_list)}`):\n" + ("\n".join([f"{i+1}. {r}" for i, r in enumerate(r_list)]) if r_list else "Нет"))

    elif cmd in ["гснятьвыговор", "gunreprimand"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and db.get("global_reprimands", {}).get(str(tid)):
            db["global_reprimands"][str(tid)].pop()
            save_db(db)
            return await message.answer(f"🌍🟢 Сетевой выговор снят у {await format_vk_user(tid)}.")

    elif cmd in ["гсозыв", "gcall"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        msg_call = " ".join(params).strip() if params else "Сетевой созыв!"
        sent = 0
        for cid in db.get("global_chats", []):
            try:
                await bot.api.messages.send(peer_id=int(cid), message=f"🌍📣 **СЕТЕВОЙ СОЗЫВ**: **{msg_call}**\n\n@all", random_id=random.randint(1, 2_000_000_000))
                sent += 1
            except Exception:
                pass
        return await message.answer(f"🌍📣 Отправлено в `{sent}` бесед.")

    elif cmd in ["грассылка", "gbroadcast"]:
        if not is_main_owner and user_lvl < 95:
            return await message.answer("❌ Только владелец бота или 95+ lvl.")
        body = " ".join(params).strip()
        if not body:
            return await message.answer("❌ Формат: `/грассылка [текст]`")
        return await message.answer(f"🚀 Рассылка отправлена в `{await run_global_broadcast(uid_int, body)}` бесед.")

    elif cmd in ["gcheck", "гпроверка"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        tid = tid or uid_int
        t_str = str(tid)
        return await message.answer(
            f"🌍 Досье {await format_vk_user(tid)}:\n"
            f"• Глобальный lvl: `{db.get('global_roles', {}).get(t_str, 0)}`\n"
            f"• Сетевых выговоров: `{len(db.get('global_reprimands', {}).get(t_str, []))}`\n"
            f"• Глобальный бан: `{db.get('global_bans', {}).get(t_str) or 'Нет'}`"
        )

    elif cmd in ["gtop", "гтоп"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        totals = {}
        for c_info in db.get("chats", {}).values():
            for u_id, val in c_info.get("reputation", {}).items():
                totals[u_id] = totals.get(u_id, 0) + int(val)
        top_net = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:10]
        return await message.answer("🌍 **ТОП РЕПУТАЦИИ:**\n\n" + ("\n".join([f"{i+1}. {await format_vk_user(int(u))} — **`{v}`** ⭐" for i, (u, v) in enumerate(top_net)]) if top_net else "Пусто"))

    elif cmd in ["gstats", "гстата", "gchats", "гчаты"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        return await message.answer(
            f"🌍 Чатов: `{len(db.get('global_chats', []))}` | Активных в пуле: `{count_active_pool_chats()}` | Глобальных банов: `{len(db.get('global_bans', {}))}`"
        )

    elif cmd in ["admins", "админс"]:
        lines = []
        shown = set()
        if owner_id:
            shown.add(int(owner_id))
            lines.append(f"👑 **Владелец чата:** {await format_vk_user(int(owner_id))} ➔ `100 lvl`")
        for m_id, info in chat_data.get("members", {}).items():
            u_item = int(m_id)
            if u_item in shown:
                continue
            lvl = info.get("level", 0) if isinstance(info, dict) else int(info or 0)
            if lvl > 0:
                shown.add(u_item)
                lines.append(f"• {await format_vk_user(u_item)} ➔ **{get_role_title(lvl, chat_data)}** `[{lvl} lvl]`")
        return await message.answer("👑 ═══ АДМИНИСТРАЦИЯ ═══ 👑\n\n" + ("\n".join(lines) if lines else "Админов пока нет."), keyboard=get_chat_inline_keyboard())

    elif cmd in ["staff", "состав"]:
        parts_out = []
        if owner_id:
            parts_out.append(f"👑 **Владелец:** {await format_vk_user(int(owner_id))} (`100 lvl`)")
        adm_lines = [f"• {await format_vk_user(int(m_id))} — **{get_role_title(info.get('level', 0) if isinstance(info, dict) else int(info or 0), chat_data)}**" for m_id, info in chat_data.get("members", {}).items() if (info.get("level", 0) if isinstance(info, dict) else int(info or 0)) > 0 and int(m_id) != int(owner_id or 0)]
        if adm_lines:
            parts_out.append("🛡 **Администраторы:**\n" + "\n".join(adm_lines))
        if db.get("leaders"):
            parts_out.append("👔 **Лидеры:**\n" + "\n".join([f"• {await format_vk_user(int(l_id))} — **{org}**" for l_id, org in db["leaders"].items()]))
        return await message.answer("📋 ═══ СОСТАВ ═══ 📋\n\n" + ("\n\n".join(parts_out) if parts_out else "Пусто"))


# =====================================================================
# 9. ЗАПУСК (FLASK ДЛЯ RENDER + VKBOTTLE POLLING)
# =====================================================================
async def main() -> None:
    start_flask_server()
    print(f"[BOT START] {DEFAULT_PROJECT_NAME} | Port: {PORT} | DB: {DB_FILE}")
    await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
