import os
import json
import time
import secrets
import asyncio
from threading import Thread
from flask import Flask
from vkbottle.bot import Bot, Message
from vkbottle import Keyboard, KeyboardButtonColor, Text

# =====================================================================
# 1. НАСТРОЙКИ СБОРКИ (МЕНЯЮТСЯ В ОДНОМ МЕСТЕ ПОД ЛЮБОЙ ПРОЕКТ)
# =====================================================================
TOKEN = "vk1.a.5BfnzsvcbvRGCC9ZGxmbKdzOU-0NBWIPc9RovnKuIImAHcV46i4wMJUGvrkua3xKhDqo57KVoiXQOqhuctwA93--pk54OWAy_rfzgGfTTQFdG1vFzRObXKPTikSt3h1rRR6ImqGbPMaOgAC3vLaPxx359f9T4N2nl2d4dZGFInNv4F9LLsctf3GiuJfrBkqBcji79CiXrPLqeClPoXaptg"
MAIN_OWNER_ID = 897082738  # Главный VK ID владельца бота (ЛС-панель, рандомные коды, рассылка)

DEFAULT_PROJECT_NAME = "CHAT MANAGER"
DEFAULT_PROJECT_URL = "https://vk.com"
DB_FILE = "chat_manager_db.json"

MAX_CHATS_PER_CODE = 1           # 1 рандомный код = 1 беседа (подключение строго по 1 коду)
MAX_ACTIVE_POOL_CHATS = 15       # Максимум бесед в одном активном пуле владельца
REQUIRE_GLOBAL_POOL = True       # True = команды /г... и /g... работают ТОЛЬКО по активированному коду пула
REQUIRE_NICK_ROLES_POOL = True   # True = отдельный пул «Ники и роли» активируется кодом или командой /пулники

DEFAULT_ROLES = {
    100: "👑 Владелец / Основатель",
    95: "💎 Зам. Основателя",
    90: "🚀 Руководитель",
    88: "⚡ Зам. Руководителя",
    80: "🛡 Спец. Администратор",
    77: "🔥 Красная Администрация",
    75: "⭐ Зам. Спец. Админа",
    70: "🛠 Старший Администратор",
    60: "📌 Главный Администратор",
    55: "🔸 Зам. Главного Админа",
    45: "🔹 Куратор / Модератор",
    30: "🎫 Младший Модератор",
    15: "🌱 Хелпер / Стажёр"
}

DEFAULT_RULES = (
    "📜 ═══ РЕГЛАМЕНТ БЕСЕДЫ ═══ 📜\n"
    "1. Запрещён спам, флуд и сторонние рекламные ссылки.\n"
    "2. Уважительное отношение к администрации и участникам.\n"
    "3. Оскорбления, провокации и сливы — наказание по регламенту.\n"
    "4. Лимит предупреждений: 3 варна ➔ автоматический кик."
)


# =====================================================================
# 2. ВЕБ-СЕРВЕР ДЛЯ РАБОТЫ 24/7 (RENDER / UPTIMEROBOT)
# =====================================================================
app = Flask(__name__)
START_TIME = time.time()

@app.route('/')
def home():
    uptime_sec = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_sec, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{DEFAULT_PROJECT_NAME} Server: ONLINE 24/7 | Uptime: {hours}h {minutes}m {seconds}s"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

Thread(target=run_flask, daemon=True).start()


# =====================================================================
# 3. ИНИЦИАЛИЗАЦИЯ БОТА И БАЗЫ ДАННЫХ
# =====================================================================
bot = Bot(token=TOKEN)

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_db(data):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения БД: {e}")

db = load_db()
db.setdefault("chats", {})
db.setdefault("global_chats", [])
db.setdefault("broadcast_admins", [MAIN_OWNER_ID])
db.setdefault("global_roles", {})
db.setdefault("global_bans", {})
db.setdefault("leaders", {})
db.setdefault("reports", [])
db.setdefault("logs", [])
db.setdefault("pool_codes", {})

user_states = {}
user_cache = {}
flood_tracker = {}      # {(peer_id, uid): [timestamps]}
rep_cooldown = {}       # {(peer_id, uid): last_ts}

def add_log(text: str):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    entry = f"{timestamp} {text}"
    db["logs"].append(entry)
    if len(db["logs"]) > 300:
        db["logs"].pop(0)
    save_db(db)

def get_chat_data(peer_id: int) -> dict:
    p_str = str(peer_id)
    if p_str not in db["chats"]:
        db["chats"][p_str] = {
            "members": {},
            "bans": {},
            "warns": {},
            "reprimands": {},   # Выговоры админам
            "reputation": {},   # Репутация участников
            "mutes": {},
            "notes": {},
            "role_names": {str(k): v for k, v in DEFAULT_ROLES.items()},
            "rules_text": DEFAULT_RULES,
            "greeting_text": "",
            "project_name": DEFAULT_PROJECT_NAME,
            "project_url": DEFAULT_PROJECT_URL,
            "welcomed": False,
            "owner_id": None,
            "antispam": False,
            "caps_guard": False,
            "antiflood": False,
            "locked": False,
            "pools": {
                "global_cmds": False,
                "nick_roles": False,
                "global_code": None,
                "nick_roles_code": None
            }
        }
    chat = db["chats"][p_str]
    chat.setdefault("members", {})
    chat.setdefault("bans", {})
    chat.setdefault("warns", {})
    chat.setdefault("reprimands", {})
    chat.setdefault("reputation", {})
    chat.setdefault("mutes", {})
    chat.setdefault("notes", {})
    chat.setdefault("role_names", {str(k): v for k, v in DEFAULT_ROLES.items()})
    chat.setdefault("rules_text", DEFAULT_RULES)
    chat.setdefault("greeting_text", "")
    chat.setdefault("project_name", DEFAULT_PROJECT_NAME)
    chat.setdefault("project_url", DEFAULT_PROJECT_URL)
    chat.setdefault("welcomed", False)
    chat.setdefault("owner_id", None)
    chat.setdefault("antispam", False)
    chat.setdefault("caps_guard", False)
    chat.setdefault("antiflood", False)
    chat.setdefault("locked", False)
    chat.setdefault("pools", {
        "global_cmds": False,
        "nick_roles": False,
        "global_code": None,
        "nick_roles_code": None
    })
    chat["pools"].setdefault("global_cmds", False)
    chat["pools"].setdefault("nick_roles", False)
    chat["pools"].setdefault("global_code", None)
    chat["pools"].setdefault("nick_roles_code", None)
    return chat


# =====================================================================
# 4. РАНДОМНЫЕ КОДЫ ПУЛА (1 КОД = 1 БЕСЕДА) И ПРОВЕРКИ ДОСТУПА
# =====================================================================

def count_active_pool_chats() -> int:
    cnt = 0
    for c_data in db.get("chats", {}).values():
        pools = c_data.get("pools", {})
        if pools.get("global_cmds") or pools.get("nick_roles"):
            cnt += 1
    return cnt

def generate_random_code(pool_type: str, created_by: int) -> str:
    prefix = "G1" if pool_type == "global" else ("NR1" if pool_type == "nick_roles" else "ALL1")
    while True:
        code = f"{prefix}-{secrets.token_hex(4).upper()}"
        if code not in db["pool_codes"]:
            break
    db["pool_codes"][code] = {
        "type": pool_type,  # "global" (/г... /g...), "nick_roles" (ники и роли), "all" (оба пула)
        "max_chats": MAX_CHATS_PER_CODE,  # строго 1 рандомный ключ = 1 беседа
        "chats": [],
        "created_by": created_by,
        "created_at": time.strftime("%Y-%m-%d %H:%M")
    }
    save_db(db)
    return code

def activate_pool_code_for_chat(peer_id: int, raw_code: str) -> tuple[bool, str]:
    code = raw_code.strip().upper()
    code_obj = db.get("pool_codes", {}).get(code)
    if not code_obj:
        return False, "❌ Неверный или несуществующий рандомный код активации."

    used_chats = [int(c) for c in code_obj.get("chats", [])]
    if peer_id in used_chats:
        return False, f"ℹ️ Код `{code}` уже активирован и привязан к этой беседе (`1/1`)."

    if len(used_chats) >= MAX_CHATS_PER_CODE:
        return False, f"⛔ Код `{code}` уже использован в другой беседе (`1/1`)! На каждый чат нужен свой рандомный код."

    chat_data = get_chat_data(peer_id)
    already_in_pool = bool(chat_data["pools"].get("global_cmds") or chat_data["pools"].get("nick_roles"))
    if not already_in_pool and count_active_pool_chats() >= MAX_ACTIVE_POOL_CHATS:
        return False, f"⛔ Достигнут лимит пула (`{MAX_ACTIVE_POOL_CHATS}/{MAX_ACTIVE_POOL_CHATS}` активных бесед)."

    used_chats.append(peer_id)
    code_obj["chats"] = used_chats

    pools = chat_data["pools"]
    p_type = code_obj.get("type", "global")

    activated_names = []
    if p_type in ("global", "all"):
        pools["global_cmds"] = True
        pools["global_code"] = code
        activated_names.append("🌐 Пул сетевых команд (`/г...` и `/g...`)")
    if p_type in ("nick_roles", "all"):
        pools["nick_roles"] = True
        pools["nick_roles_code"] = code
        activated_names.append("🎭 Отдельный пул «Ники и роли»")

    save_db(db)
    add_log(f"Активирован рандомный код {code} ({p_type}) в чате {peer_id} [1/1]")
    return True, (
        f"✅ **Код `{code}` успешно активирован (`1/1`)!**\n"
        f"• Подключено: {' + '.join(activated_names)}\n"
        f"• Занято мест в общем пуле: `{count_active_pool_chats()}/{MAX_ACTIVE_POOL_CHATS}`"
    )

def has_global_pool(chat_data: dict) -> bool:
    if not REQUIRE_GLOBAL_POOL:
        return True
    pools = chat_data.get("pools", {})
    if not pools.get("global_cmds"):
        return False
    code = pools.get("global_code")
    if code and code in db.get("pool_codes", {}):
        return True
    return bool(pools.get("global_cmds"))

def has_nick_roles_pool(chat_data: dict) -> bool:
    if not REQUIRE_NICK_ROLES_POOL:
        return True
    pools = chat_data.get("pools", {})
    if not pools.get("nick_roles"):
        return False
    code = pools.get("nick_roles_code")
    if code and code in db.get("pool_codes", {}):
        return True
    return bool(pools.get("nick_roles"))


# =====================================================================
# 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ЧАТА И VK API
# =====================================================================

def build_welcome_guide(chat_data: dict) -> str:
    proj = chat_data.get("project_name", DEFAULT_PROJECT_NAME)
    return (
        f"👋 ═══ ПРИВЕТ! {proj} В БЕСЕДЕ ═══ 👋\n\n"
        "👑 **Владелец чата получает `100 lvl` админки**.\n"
        "Если создатель не определился автоматически — команду `/явладелец` может прописать только **создатель или администратор беседы (со звездой)**.\n"
        "📌 *Совет:* выдай боту права администратора в беседе для киков, банов, мутов, очистки и закрепа.\n\n"
        "⚡ **Быстрые команды:**\n"
        "• `/menu` или `меню` — интерактивные кнопки в беседе\n"
        "• `/help` или `/хелп` — удобный справочник по всем командам\n"
        "• `/код [КЛЮЧ]` — активировать рандомный код пула (`1 код = 1 чат`)\n"
        "• `/пул` — проверить статус пулов беседы"
    )

def sync_owner_member(chat_data: dict, owner_id: int | None):
    if owner_id and int(owner_id) > 0:
        oid = int(owner_id)
        chat_data["owner_id"] = oid
        o_str = str(oid)
        current_data = chat_data["members"].get(o_str, {})
        nick = current_data.get("nickname", "") if isinstance(current_data, dict) else ""
        chat_data["members"][o_str] = {
            "level": 100,
            "nickname": nick,
            "is_owner": True
        }

async def get_vk_chat_member_flags(peer_id: int, target_uid: int) -> tuple[bool, bool]:
    """Проверяет is_owner (создатель) и is_admin (серая звезда) через VK API."""
    try:
        members_res = await bot.api.messages.get_conversation_members(peer_id=peer_id)
        if members_res and getattr(members_res, "items", None):
            for item in members_res.items:
                if int(getattr(item, "member_id", 0)) == int(target_uid):
                    is_own = bool(getattr(item, "is_owner", False))
                    is_adm = bool(getattr(item, "is_admin", False))
                    return is_own, (is_own or is_adm)
    except Exception:
        pass
    return False, False

async def resolve_chat_owner(peer_id: int, chat_data: dict):
    title = f"Беседа {peer_id}"
    vk_owner_id = None

    try:
        chat_info = await bot.api.messages.get_conversations_by_peer_ids(peer_ids=[peer_id])
        if chat_info and chat_info.items:
            conv = chat_info.items[0]
            settings = getattr(conv, "chat_settings", None)
            if settings:
                if getattr(settings, "title", None):
                    title = settings.title
                raw_owner = getattr(settings, "owner_id", None)
                if raw_owner is not None and int(raw_owner) > 0:
                    vk_owner_id = int(raw_owner)
    except Exception:
        pass

    if not vk_owner_id:
        try:
            members_res = await bot.api.messages.get_conversation_members(peer_id=peer_id)
            if members_res and getattr(members_res, "items", None):
                for item in members_res.items:
                    if getattr(item, "is_owner", False):
                        m_id = int(getattr(item, "member_id", 0))
                        if m_id > 0:
                            vk_owner_id = m_id
                            break
        except Exception:
            pass

    if vk_owner_id:
        chat_data["owner_id"] = vk_owner_id

    owner_id = chat_data.get("owner_id")
    if owner_id:
        sync_owner_member(chat_data, int(owner_id))

    return title, int(owner_id) if owner_id else None

def is_chat_owner(uid: int, chat_data: dict, owner_id: int | None) -> bool:
    if owner_id and int(uid) == int(owner_id):
        return True
    saved_owner = chat_data.get("owner_id")
    if saved_owner and int(uid) == int(saved_owner):
        return True
    return False

def get_user_level_by_id(uid: int, chat_data: dict, owner_id: int | None) -> int:
    if is_chat_owner(uid, chat_data, owner_id):
        return 100

    m_info = chat_data.get("members", {}).get(str(uid), 0)
    if isinstance(m_info, dict):
        lvl = int(m_info.get("level", 0))
        if lvl > 0:
            return lvl
    elif isinstance(m_info, (int, float)):
        lvl = int(m_info)
        if lvl > 0:
            return lvl

    if str(uid) in db.get("global_roles", {}):
        return int(db["global_roles"][str(uid)])

    return 0

def get_role_title(lvl: int, chat_data: dict, owner_flag: bool = False) -> str:
    chat_roles = chat_data.get("role_names", {})
    if owner_flag or lvl >= 100:
        return chat_roles.get("100") or chat_roles.get(100) or "👑 Владелец / Основатель"
    if lvl <= 0:
        return "Участник"
    return chat_roles.get(str(lvl)) or chat_roles.get(lvl) or "Администратор"

async def format_vk_user(uid: int) -> str:
    if uid <= 0:
        return f"ID {uid}"
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
    if message.reply_message:
        return message.reply_message.from_id, args
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
        .replace("vk.com/", "")
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
    kb.add(Text("👑 Админы"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("📊 Стата"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("📋 Роли"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("📜 Правила"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("🔑 Код пула"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("📦 Статус пула"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("🛠 Модерация"), color=KeyboardButtonColor.NEGATIVE)
    kb.add(Text("📖 Команды"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


# =====================================================================
# 6. ЛИЧНЫЕ СООБЩЕНИЯ (ПАНЕЛЬ УПРАВЛЕНИЯ + РАНДОМНЫЕ КОДЫ 1-НА-1 ЧАТ)
# =====================================================================

def get_private_keyboard(uid: int) -> str:
    is_main = (uid == MAIN_OWNER_ID)
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
        kb.add(Text("🎲 Код /г... (1 чат)"), color=KeyboardButtonColor.POSITIVE)
        kb.add(Text("🎭 Код Ники/Роли (1 чат)"), color=KeyboardButtonColor.POSITIVE)
        kb.row()
        kb.add(Text("⭐ Полный код (1 чат)"), color=KeyboardButtonColor.PRIMARY)
        kb.add(Text("📦 Пачка кодов (5 шт)"), color=KeyboardButtonColor.PRIMARY)
        kb.row()
        kb.add(Text("📋 Все коды"), color=KeyboardButtonColor.SECONDARY)
        kb.add(Text("🗑 Удалить код"), color=KeyboardButtonColor.NEGATIVE)

    return kb.get_json()

@bot.on.private_message(text=["/start", "Начать", "начать", "Меню", "меню", "🔙 Главное меню", "❌ Отмена"])
async def private_start(message: Message):
    uid = message.from_id
    user_states.pop(uid, None)
    await message.answer(
        f"👑 ═══ {DEFAULT_PROJECT_NAME} — ПАНЕЛЬ ВЛАДЕЛЬЦА ═══ 👑\n\n"
        f"• Рандомные коды пула: `1 код = 1 беседа` (активно мест: `{count_active_pool_chats()}/{MAX_ACTIVE_POOL_CHATS}`).\n"
        "• Пул `/г...` и `/g...` открывает сетевые команды.\n"
        "• Пул «Ники и роли» открывает кастомные ники и редактирование ролей.",
        keyboard=get_private_keyboard(uid)
    )

@bot.on.private_message(text="🎲 Код /г... (1 чат)")
async def private_gen_global_1(message: Message):
    if message.from_id != MAIN_OWNER_ID:
        return
    code = generate_random_code("global", message.from_id)
    await message.answer(
        f"🎲 **Рандомный код для пула `/г...` и `/g...` создан!**\n\n"
        f"• Ключ: `{code}`\n"
        f"• Привязка: `0/1` беседа\n"
        f"• Команда в беседе: `/код {code}`",
        keyboard=get_private_keyboard(message.from_id)
    )

@bot.on.private_message(text="🎭 Код Ники/Роли (1 чат)")
async def private_gen_nr_1(message: Message):
    if message.from_id != MAIN_OWNER_ID:
        return
    code = generate_random_code("nick_roles", message.from_id)
    await message.answer(
        f"🎭 **Рандомный код для пула «Ники и роли» создан!**\n\n"
        f"• Ключ: `{code}`\n"
        f"• Привязка: `0/1` беседа\n"
        f"• Команда в беседе: `/код {code}`",
        keyboard=get_private_keyboard(message.from_id)
    )

@bot.on.private_message(text="⭐ Полный код (1 чат)")
async def private_gen_all_1(message: Message):
    if message.from_id != MAIN_OWNER_ID:
        return
    code = generate_random_code("all", message.from_id)
    await message.answer(
        f"⭐ **Полный рандомный код (Оба пула) создан!**\n\n"
        f"• Ключ: `{code}`\n"
        f"• Привязка: `0/1` беседа\n"
        f"• Команда в беседе: `/код {code}`",
        keyboard=get_private_keyboard(message.from_id)
    )

@bot.on.private_message(text="📦 Пачка кодов (5 шт)")
async def private_gen_batch_5(message: Message):
    if message.from_id != MAIN_OWNER_ID:
        return
    generated = [generate_random_code("all", message.from_id) for _ in range(5)]
    formatted = "\n".join([f"{idx + 1}. `/код {c}`" for idx, c in enumerate(generated)])
    await message.answer(
        f"📦 **Выпущена пачка из 5 рандомных кодов (по 1 чату на каждый):**\n\n{formatted}",
        keyboard=get_private_keyboard(message.from_id)
    )

@bot.on.private_message(text=["📋 Все коды", "📦 Все коды"])
async def private_list_codes(message: Message):
    if message.from_id != MAIN_OWNER_ID:
        return
    codes = db.get("pool_codes", {})
    if not codes:
        return await message.answer("📋 Выданных кодов пока нет.")
    lines = []
    type_labels = {
        "global": "🌐 /г... и /g...",
        "nick_roles": "🎭 Ники и роли",
        "all": "⭐ Оба пула"
    }
    for code, info in list(codes.items())[-30:]:
        used = len(info.get("chats", []))
        t_lbl = type_labels.get(info.get("type", "global"), info.get("type"))
        st = "🔒 Занят" if used >= MAX_CHATS_PER_CODE else "🟢 Свободен"
        lines.append(f"• `{code}` | {t_lbl} | `{used}/{MAX_CHATS_PER_CODE}` ({st})")
    await message.answer(
        f"📋 **РАНДОМНЫЕ КОДЫ (`{count_active_pool_chats()}/{MAX_ACTIVE_POOL_CHATS}` мест в пуле):**\n\n" + "\n".join(lines)
    )

@bot.on.private_message(text="🗑 Удалить код")
async def private_del_code_start(message: Message):
    if message.from_id != MAIN_OWNER_ID:
        return
    user_states[message.from_id] = "wait_del_code"
    await message.answer(
        "🗑 Введите код, который нужно отозвать и удалить:",
        keyboard=Keyboard(one_time=False, inline=True).add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE).get_json()
    )

@bot.on.private_message(text=["➕ Админ рассылки", "➕ Выдать доступ к рассылке"])
async def private_add_adm(message: Message):
    uid = message.from_id
    if uid != MAIN_OWNER_ID and uid not in db["broadcast_admins"]:
        return
    user_states[uid] = "wait_add_adm"
    await message.answer(
        "✍️ Введите цифровой VK ID или @ник пользователя для выдачи доступа к глобальной рассылке:",
        keyboard=Keyboard(one_time=False, inline=True).add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE).get_json()
    )

@bot.on.private_message(text="📜 Логи рассылок")
async def private_view_logs(message: Message):
    uid = message.from_id
    if uid != MAIN_OWNER_ID and uid not in db["broadcast_admins"]:
        return
    broadcast_logs = [log for log in db["logs"] if "рассылка" in log.lower() or "рассылки" in log.lower()]
    recent = "\n".join(broadcast_logs[-10:]) if broadcast_logs else "📋 Логов рассылок пока нет."
    await message.answer(f"📜 **ИСТОРИЯ ОФИЦИАЛЬНЫХ РАССЫЛОК:**\n\n{recent}")

@bot.on.private_message(text=["👥 Список рассылки", "👥 Список админов рассылки"])
async def private_view_admins(message: Message):
    uid = message.from_id
    if uid != MAIN_OWNER_ID and uid not in db["broadcast_admins"]:
        return
    lines = [f"• {await format_vk_user(aid)}" for aid in db["broadcast_admins"]]
    await message.answer("👥 **ДОВЕРЕННЫЕ АДМИНЫ РАССЫЛКИ:**\n\n" + "\n".join(lines))

@bot.on.private_message(text=["📢 Рассылка по чатам", "📢 Начать рассылку по чатам"])
async def private_broadcast_start(message: Message):
    uid = message.from_id
    if uid != MAIN_OWNER_ID and uid not in db["broadcast_admins"]:
        return
    user_states[uid] = "wait_broadcast"
    await message.answer(
        "✍️ Введите текст для массовой отправки по всем подключённым беседам:",
        keyboard=Keyboard(one_time=False, inline=True).add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE).get_json()
    )

@bot.on.private_message(text=["🌐 Ссылка проекта", "🌐 Сайт проекта"])
async def private_site(message: Message):
    await message.answer(f"🌐 Ссылка проекта: {DEFAULT_PROJECT_URL}")

@bot.on.private_message()
async def private_handler(message: Message):
    uid = message.from_id
    text = message.text
    if not text:
        return
    state = user_states.get(uid)

    if state == "wait_del_code" and uid == MAIN_OWNER_ID:
        del user_states[uid]
        code = text.strip().upper()
        code_obj = db.get("pool_codes", {}).pop(code, None)
        if not code_obj:
            return await message.answer(f"❌ Код `{code}` не найден.", keyboard=get_private_keyboard(uid))
        for cid in code_obj.get("chats", []):
            c_data = get_chat_data(int(cid))
            if c_data["pools"].get("global_code") == code:
                c_data["pools"]["global_cmds"] = False
                c_data["pools"]["global_code"] = None
            if c_data["pools"].get("nick_roles_code") == code:
                c_data["pools"]["nick_roles"] = False
                c_data["pools"]["nick_roles_code"] = None
        save_db(db)
        return await message.answer(f"🗑 Код `{code}` удалён и отключён.", keyboard=get_private_keyboard(uid))

    if state == "wait_add_adm" and (uid == MAIN_OWNER_ID or uid in db["broadcast_admins"]):
        del user_states[uid]
        tid, _ = await resolve_target(message, [text.strip()])
        if tid and tid > 0:
            if tid not in db["broadcast_admins"]:
                db["broadcast_admins"].append(tid)
                save_db(db)
                u_fmt = await format_vk_user(tid)
                add_log(f"Админ {await format_vk_user(uid)} выдал права рассылки для {u_fmt}")
                return await message.answer(f"✅ Успешно! {u_fmt} добавлен в список рассыльщиков.", keyboard=get_private_keyboard(uid))
            return await message.answer("ℹ️ Этот пользователь уже в списке.", keyboard=get_private_keyboard(uid))
        return await message.answer("❌ Ошибка: укажите корректный VK ID или @ник.", keyboard=get_private_keyboard(uid))

    if state == "wait_broadcast" and (uid == MAIN_OWNER_ID or uid in db["broadcast_admins"]):
        del user_states[uid]
        count = 0
        broadcast_text = (
            "📢 **ВАЖНОЕ ОБЪЯВЛЕНИЕ** 📢\n\n"
            f"{text}\n\n"
            "═══════════════════════════\n"
            f"👤 Отправитель: {await format_vk_user(uid)}\n"
            "@all"
        )
        for cid in db.get("global_chats", []):
            try:
                await bot.api.messages.send(peer_id=int(cid), message=broadcast_text, random_id=0)
                count += 1
            except Exception:
                pass
        add_log(f"Массовая рассылка выполнена {await format_vk_user(uid)}. Охвачено чатов: {count}")
        return await message.answer(f"🚀 **Рассылка успешно завершена!**\n• Доставлено в беседы: `{count}`", keyboard=get_private_keyboard(uid))

    await message.answer(
        "👋 Используй кнопки ниже для выпуска рандомных кодов (1 код = 1 чат) и управления рассылками.",
        keyboard=get_private_keyboard(uid)
    )


# =====================================================================
# 7. РАБОТА В БЕСЕДАХ (ВСЁ РАБОТАЕТ: ПУЛЫ, НИКИ, РОЛИ, ВЫГОВОРЫ, РЕП, МОД)
# =====================================================================

@bot.on.chat_message()
async def chat_handler(message: Message):
    peer_id = message.peer_id
    uid_int = message.from_id
    uid_str = str(uid_int)
    chat_data = get_chat_data(peer_id)

    is_new_chat = peer_id not in db["global_chats"]
    if is_new_chat:
        db["global_chats"].append(peer_id)

    chat_title, owner_id = await resolve_chat_owner(peer_id, chat_data)
    save_db(db)

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
            try:
                await bot.api.messages.remove_chat_user(chat_id=message.chat_id, user_id=j_id)
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
                    delete_for_all=True
                )
            except Exception:
                pass
            return
        else:
            del chat_data["mutes"][uid_str]
            save_db(db)

    text = message.text
    if not text:
        return

    raw_text = text.strip()
    clean_text = raw_text.lower()
    proj_name = chat_data.get("project_name", DEFAULT_PROJECT_NAME)
    proj_url = chat_data.get("project_url", DEFAULT_PROJECT_URL)
    user_lvl = get_user_level_by_id(uid_int, chat_data, owner_id)

    # --- Закрытый чат (/лок) ---
    if chat_data.get("locked") and user_lvl < 45 and not clean_text.startswith("/"):
        try:
            await bot.api.messages.delete(
                peer_id=peer_id,
                conversation_message_ids=[message.conversation_message_id],
                delete_for_all=True
            )
        except Exception:
            pass
        return

    # --- Антифлуд (больше 5 сообщений за 6 секунд) ---
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
                    delete_for_all=True
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
                    delete_for_all=True
                )
                await message.answer(f"🛡 Антиспам удалил ссылку от {await format_vk_user(uid_int)}.")
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
                    delete_for_all=True
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

    # --- Быстрый плюсик репутации (+реп) ---
    if clean_text.startswith("+реп"):
        rem_part = raw_text[4:].strip().split()
        raw_text = ("/реп " + " ".join(rem_part)).strip()
        clean_text = raw_text.lower()

    # --- Кнопки и быстрые фразы ---
    if clean_text in ["/menu", "/panel", "/панель", "меню", "панель"]:
        return await message.answer(f"👑 ═══ ПАНЕЛЬ УПРАВЛЕНИЯ — {proj_name} ═══ 👑", keyboard=get_chat_inline_keyboard())

    if clean_text in ["🔑 код пула", "код пула"]:
        return await message.answer(
            "🔑 ═══ АКТИВАЦИЯ РАНДОМНОГО КОДА ═══ 🔑\n\n"
            "Введите команду: `/код [ВАШ_КЛЮЧ]` или `/активировать [ВАШ_КЛЮЧ]`\n"
            "• На каждую беседу выдаётся свой рандомный код (`1 код = 1 беседа`).\n"
            "• Пул `/г...` и `/g...` открывает сетевые команды.\n"
            "• Отдельный пул «Ники и роли» открывает кастомные ники и настройку ролей.",
            keyboard=get_chat_inline_keyboard()
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
            f"• Формат ключей: `1 рандомный код = 1 беседа`\n"
            f"• Занято мест в общем пуле: `{count_active_pool_chats()}/{MAX_ACTIVE_POOL_CHATS}`",
            keyboard=get_chat_inline_keyboard()
        )

    if clean_text in ["🌐 ссылка", "🌐 сайт", "ссылка", "сайт", "/link", "/ссылка"]:
        return await message.answer(f"🌐 Ссылка проекта **{proj_name}**: {proj_url}", keyboard=get_chat_inline_keyboard())

    if clean_text in ["📜 правила", "правила", "/rules", "/правила"]:
        return await message.answer(chat_data.get("rules_text", DEFAULT_RULES), keyboard=get_chat_inline_keyboard())

    if clean_text in ["🛠 модерация", "модерация"]:
        return await message.answer(
            "🛠 ═══ ШПАРГАЛКА МОДЕРАЦИИ ═══ 🛠\n\n"
            "• `15+ lvl`: `/заметка`, `/удзаметку`\n"
            "• `45+ lvl`: `/мут`, `/анмут`, `/варн`, `/варны`, `/анварн`, `/кик`, `/очистить`, `/закреп`, `/откреп`, `/лок`, `/анлок`\n"
            "• `55+ lvl`: `/бан`, `/анбан`, `/банлист`, `/чекбан`, `/снятьвсеварны`, `/статачата`\n"
            "• `60+ lvl`: `/сник`, `/делник` *(пул)*, `/сетлидер`, `/снятлидера`, `/репорты`, `/ответить`, `/созыв`\n"
            "• `70+ lvl`: `/сетадмин`, `/снятьроль` *(пул)*, `/выговор`, `/выговоры`, `/снятьвыговор`, `/сетправила`, `/сетпривет`, `/размутвсех`, `/антиспам`, `/антикапс`, `/антифлуд`, `/логи`\n"
            "• `90+ lvl`: `/новаяроль`, `/удалитьроль`, `/сбросролей` *(пул)*, `/сетназвание`, `/сетссылка`, `/сетвладелец`, `/сбросвладельца`\n"
            "• `Пул /г... и /g...`: `/гбан`, `/гунбан`, `/гбанлист`, `/гчекбан`, `/гроль`, `/снятьгроль`, `/гпроверка`, `/гстата`, `/гчаты`, `/гтоп`",
            keyboard=get_chat_inline_keyboard()
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

    if clean_text in ["👑 админы", "/admins", "/админс", "админы", "админс"]:
        clean_text = "/admins"
        raw_text = "/admins"

    if clean_text in ["📊 стата", "/stats", "/стата", "/профиль", "стата"]:
        clean_text = "/stats"
        raw_text = "/stats"

    if clean_text in ["📖 команды", "/help", "/хелп", "команды", "хелп"]:
        clean_text = "/help"
        raw_text = "/help"

    if not clean_text.startswith("/"):
        return

    args = raw_text[1:].split()
    cmd = args[0].lower() if args else ""
    params = args[1:] if len(args) > 1 else []

    # =================================================================
    # ЖЁСТКАЯ ПРОВЕРКА ПУЛА ДЛЯ ВСЕХ КОМАНД НА /г... И /g...
    # =================================================================
    if cmd.startswith("г") or cmd.startswith("g"):
        if not has_global_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer(
                f"🔒 **Команда `/{cmd}` относится к пулу `/г...` и `/g...`!**\n"
                "Для этой беседы рандомный код пула ещё не активирован (`1 код = 1 беседа`).\n\n"
                "👉 Активировать ключ: `/код [КЛЮЧ]` или кнопка **«🔑 Код пула»**.",
                keyboard=get_chat_inline_keyboard()
            )

    # =================================================================
    # КОМАНДЫ АКТИВАЦИИ И УПРАВЛЕНИЯ ПУЛАМИ
    # =================================================================
    if cmd in ["код", "code", "активировать", "activate"]:
        if not params:
            return await message.answer("❌ Формат: `/код [РАНДОМНЫЙ_КЛЮЧ]`")
        ok, reply_msg = activate_pool_code_for_chat(peer_id, params[0])
        return await message.answer(reply_msg, keyboard=get_chat_inline_keyboard())

    elif cmd in ["выдатькод", "genrand", "создатькод"]:
        if uid_int != MAIN_OWNER_ID:
            return await message.answer("❌ Генерация рандомных кодов доступна только владельцу бота.")
        p_type = params[0].lower() if params else "all"
        if p_type not in ("global", "nick_roles", "all"):
            return await message.answer("❌ Формат: `/выдатькод [global|nick_roles|all]`")
        code = generate_random_code(p_type, uid_int)
        return await message.answer(f"🎲 Выдан рандомный код (`1` беседа): `{code}` (`{p_type}`)")

    elif cmd in ["пулники", "nickpool"]:
        if uid_int != MAIN_OWNER_ID and user_lvl < 95:
            return await message.answer("❌ Требуется владелец бота или 95+ lvl.")
        chat_data["pools"]["nick_roles"] = not bool(chat_data["pools"].get("nick_roles"))
        save_db(db)
        st = "🟢 ВКЛЮЧЁН" if chat_data["pools"]["nick_roles"] else "🔴 ВЫКЛЮЧЕН"
        return await message.answer(f"🎭 Отдельный пул «Ники и роли» в этой беседе: {st}.")

    # =================================================================
    # УМНЫЙ МНОГОСТРАНИЧНЫЙ /HELP
    # =================================================================
    elif cmd in ["help", "хелп", "cmds", "команды"]:
        section = params[0].lower() if params else ""

        if section in ["мод", "mod", "модерация"]:
            return await message.answer(
                "🛡 ═══ КОМАНДЫ МОДЕРАЦИИ ═══ 🛡\n\n"
                "• `/мут` `[@цель] [мин]` (45+) — замутить с удалением сообщений\n"
                "• `/анмут` `[@цель]` (45+) / `/размутвсех` (70+) — снять мут\n"
                "• `/варн` `[@цель] [причина]` (45+) — выдать варн (3 = кик)\n"
                "• `/варны` `[@цель]` / `/анварн` (45+) / `/снятьвсеварны` (55+)\n"
                "• `/кик` `[@цель]` (45+) — исключить из беседы\n"
                "• `/бан` `[@цель] [причина]` (55+) / `/анбан` (55+) / `/банлист` / `/чекбан`\n"
                "• `/выговор` `[@цель] [причина]` (70+) — выговор админу (3 = снятие)\n"
                "• `/выговоры` `[@цель]` / `/снятьвыговор` (70+) / `/снятьвсевыговоры` (70+)\n"
                "• `/очистить` `[1-50]` (45+) — удалить последние сообщения\n"
                "• `/закреп` (реплай) / `/откреп` (45+)\n"
                "• `/лок` / `/анлок` (45+) — закрыть/открыть беседу\n"
                "• `/антиспам` / `/антикапс` / `/антифлуд` (70+)",
                keyboard=get_chat_inline_keyboard()
            )

        if section in ["роли", "ники", "roles", "nicks"]:
            return await message.answer(
                "🎭 ═══ ОТДЕЛЬНЫЙ ПУЛ «НИКИ И РОЛИ» ═══ 🎭\n\n"
                "• `/ник [текст]` — установить свой кастомный ник\n"
                "• `/нлист` — список всех ников в беседе\n"
                "• `/сник` `[@цель] [Ник]` (60+) — выдать ник участнику\n"
                "• `/делник` `[@цель]` (60+) — удалить ник участнику\n"
                "• `/сетадмин` `[@цель] [уровень]` (70+) — выдать уровень прав\n"
                "• `/снятьроль` `[@цель]` (70+) — снять права админа (0 lvl)\n"
                "• `/новаяроль` `[ур] [Название]` (90+) — создать или изменить роль\n"
                "• `/удалитьроль` `[ур]` (90+) — удалить роль из списка\n"
                "• `/сбросролей` (90+) — сбросить роли к стандартным\n"
                "• `/пулники` (95+ / владелец) — вкл/выкл пул вручную",
                keyboard=get_chat_inline_keyboard()
            )

        if section in ["пул", "pool", "г", "g"]:
            return await message.answer(
                "🌍 ═══ СЕТЕВОЙ ПУЛ `/г...` И `/g...` ═══ 🌍\n"
                "*(Работает по рандомному коду: `/код [КЛЮЧ]`, 1 код = 1 беседа)*\n\n"
                "• `/гбан` (`/gban`) `[@цель] [причина]` (90+) — глобальный бан по всей сети\n"
                "• `/гунбан` (`/gunban`) `[@цель]` (90+) — снять глобальный бан\n"
                "• `/гбанлист` (`/gbanlist`) (60+) — список глобальных банов\n"
                "• `/гчекбан` (`/gcheckban`) `[@цель]` (45+) — проверка в глобальном ЧС\n"
                "• `/гроль` (`/grole`) `[@цель] [уровень]` (95+) — выдать сетевой уровень\n"
                "• `/снятьгроль` (`/grr`) `[@цель]` (95+) — снять сетевой уровень\n"
                "• `/гпроверка` (`/gcheck`) `[@цель]` (60+) — сетевое досье игрока\n"
                "• `/гстата` (`/gstats`) / `/гчаты` (`/gchats`) (60+) — статистика сети\n"
                "• `/гтоп` (`/gtop`) (45+) — топ участников сети по репутации",
                keyboard=get_chat_inline_keyboard()
            )

        if section in ["чат", "chat", "фан", "fun"]:
            return await message.answer(
                "⚙️ ═══ НАСТРОЙКИ ЧАТА И УТИЛИТЫ ═══ ⚙️\n\n"
                "• `/явладелец` — закрепить владельца (`100 lvl`, только для создателя или админа со звездой)\n"
                "• `/сетвладелец` `[@цель]` (90+) / `/сбросвладельца` (90+)\n"
                "• `/сетназвание` `[Название]` (90+) / `/сетссылка` `[URL]` (90+)\n"
                "• `/сетправила` `[текст]` (70+) / `/сбросправил` (70+)\n"
                "• `/сетпривет` `[текст]` (70+) / `/сброспривета` (70+)\n"
                "• `/сетлидер` `[@цель] [Орг]` (60+) / `/снятлидера` (60+) / `/лидеры`\n"
                "• `/созыв` `[текст]` (60+) — созвать участников (`@all`)\n"
                "• `/репорт` `[текст]` / `/репорты` (60+) / `/ответить` `[#id] [ответ]` (60+)\n"
                "• `/заметки` / `/заметка` `[имя] | [текст]` (15+) / `/удзаметку` `[имя]` (15+)\n"
                "• `/реп` `[@цель]` (или `+реп`) / `/топреп` — репутация в чате\n"
                "• `/кто [вопрос]`, `/шанс [событие]`, `/выбор [1 / 2]`, `/мойид`, `/время`, `/пинг`, `/статус`",
                keyboard=get_chat_inline_keyboard()
            )

        return await message.answer(
            f"📖 ═══ НАВИГАЦИЯ ПО КОМАНДАМ ({proj_name}) ═══ 📖\n\n"
            "Выбери раздел справки или используй кнопки ниже:\n"
            "• `/help мод` — модерация, баны, муты, варны, выговоры, защита\n"
            "• `/help роли` — отдельный пул «Ники и роли»\n"
            "• `/help пул` — сетевой пул `/г...` и `/g...` (по рандомному коду)\n"
            "• `/help чат` — настройки проекта, лидеры, репорты, репутация, заметки\n\n"
            "⚡ **Основные команды всегда под рукой:**\n"
            "• `/menu` — кнопки | `/код [ключ]` — активация пула | `/пул` — статус\n"
            "• `/стата` — досье | `/проверка` — полная проверка | `/админс` — админы\n"
            "• `/состав` — весь состав | `/правила` — регламент | `/инфо` — о чате\n"
            "• `/явладелец` — закрепить владельца (только со звездой/создатель)",
            keyboard=get_chat_inline_keyboard()
        )

    # =================================================================
    # УТИЛИТЫ, РЕПУТАЦИЯ, ЗАМЕТКИ, СОЗЫВ
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
        last_t = rep_cooldown.get(cd_key, 0)
        if now_t - last_t < 3600 and uid_int != MAIN_OWNER_ID:
            mins_left = int((3600 - (now_t - last_t)) // 60) + 1
            return await message.answer(f"⏳ Вы уже выдавали репутацию. Повторно через `{mins_left}` мин.")
        rep_cooldown[cd_key] = now_t
        t_str = str(tid)
        chat_data["reputation"][t_str] = int(chat_data["reputation"].get(t_str, 0)) + 1
        save_db(db)
        return await message.answer(
            f"⭐ {await format_vk_user(uid_int)} повысил репутацию {await format_vk_user(tid)}! Текущая репутация: **`{chat_data['reputation'][t_str]}`**"
        )

    elif cmd in ["топреп", "toprep"]:
        reps = chat_data.get("reputation", {})
        if not reps:
            return await message.answer("⭐ В этой беседе пока ни у кого нет репутации. Выдать: `+реп @ник`.")
        sorted_rep = sorted(reps.items(), key=lambda x: int(x[1]), reverse=True)[:10]
        lines = [f"{idx + 1}. {await format_vk_user(int(u_id))} — **`{val}`** ⭐" for idx, (u_id, val) in enumerate(sorted_rep)]
        return await message.answer("🏆 ═══ ТОП РЕПУТАЦИИ БЕСЕДЫ ═══ 🏆\n\n" + "\n".join(lines))

    elif cmd in ["созыв", "call", "all"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        call_msg = " ".join(params).strip() if params else "Внимание всем участникам беседы!"
        return await message.answer(
            f"📣 **ОБЩИЙ СОЗЫВ АДМИНИСТРАЦИИ**\n"
            f"• Инициатор: {await format_vk_user(uid_int)}\n"
            f"• Сообщение: **{call_msg}**\n\n"
            "@all"
        )

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
        chosen = secrets.choice(pool_ids)
        return await message.answer(f"🎲 Я думаю, что {' '.join(params)} — это {await format_vk_user(chosen)}!")

    elif cmd in ["шанс", "chance"]:
        pct = secrets.randbelow(101)
        q = " ".join(params) if params else "этого события"
        return await message.answer(f"📊 Шанс того, что {q} — **`{pct}%`**")

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
        lines = [f"• **{k}**: {v}" for k, v in notes.items()]
        return await message.answer("📝 ═══ ЗАМЕТКИ БЕСЕДЫ ═══ 📝\n\n" + "\n".join(lines))

    elif cmd in ["заметка", "addnote"]:
        if user_lvl < 15:
            return await message.answer(f"❌ Требуется 15+ уровень (у вас: {user_lvl}).")
        body = raw_text.split(None, 1)[1].strip() if len(raw_text.split(None, 1)) > 1 else ""
        if "|" not in body:
            return await message.answer("❌ Формат: `/заметка [название] | [текст]`")
        k, v = [part.strip() for part in body.split("|", 1)]
        if not k or not v:
            return await message.answer("❌ Укажите и название, и текст заметки.")
        chat_data["notes"][k.lower()] = v
        save_db(db)
        return await message.answer(f"✅ Заметка **{k.lower()}** сохранена.")

    elif cmd in ["удзаметку", "delnote"]:
        if user_lvl < 15:
            return await message.answer(f"❌ Требуется 15+ уровень (у вас: {user_lvl}).")
        if not params:
            return await message.answer("❌ Формат: `/удзаметку [название]`")
        k = " ".join(params).strip().lower()
        if k in chat_data.get("notes", {}):
            del chat_data["notes"][k]
            save_db(db)
            return await message.answer(f"🗑 Заметка **{k}** удалена.")
        return await message.answer("❌ Заметка не найдена.")

    elif cmd in ["явладелец", "claimowner"]:
        if chat_data.get("owner_id"):
            return await message.answer(
                f"❌ Владелец в этой беседе уже закреплён: {await format_vk_user(int(chat_data['owner_id']))}."
            )
        is_vk_owner, is_vk_admin = await get_vk_chat_member_flags(peer_id, uid_int)
        if not (is_vk_owner or is_vk_admin):
            return await message.answer(
                "⛔ Отказано! Команда `/явладелец` доступна только создателю или администратору беседы ВК (со звездой)."
            )
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
            return await message.answer("❌ Формат: `/сетправила [новый текст правил беседы]`")
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
            return await message.answer("❌ Формат: `/сетназвание [Название проекта]`")
        chat_data["project_name"] = new_name[:50]
        save_db(db)
        return await message.answer(f"✅ Название проекта для этой беседы изменено на: **{chat_data['project_name']}**")

    elif cmd in ["seturl", "сетссылка"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        if not params:
            return await message.answer("❌ Формат: `/сетссылка [https://...]`")
        chat_data["project_url"] = params[0].strip()
        save_db(db)
        return await message.answer(f"✅ Ссылка проекта для этой беседы обновлена: {chat_data['project_url']}")

    elif cmd in ["ping", "пинг"]:
        start_t = time.time()
        await message.answer("🏓 Измеряем задержку...")
        end_t = time.time()
        ping_ms = int((end_t - start_t) * 1000)
        status_emoji = "🟢 Молниеносно" if ping_ms < 600 else "🟡 Стабильно" if ping_ms < 1200 else "🔴 Высокий пинг"
        return await message.answer(
            f"🏓 **PONG!**\n"
            f"• Отклик API VK: `{ping_ms} ms`\n"
            f"• Состояние: {status_emoji}"
        )

    elif cmd in ["status", "статус"]:
        uptime_sec = int(time.time() - START_TIME)
        hours, remainder = divmod(uptime_sec, 3600)
        minutes, seconds = divmod(remainder, 60)
        chats_count = len(db.get("global_chats", []))
        return await message.answer(
            f"⚡ ═══ СТАТУС ЯДРА {proj_name} ═══ ⚡\n\n"
            f"• Аптайм сервера: `{hours}ч {minutes}м {seconds}с`\n"
            f"• Подключено чатов: `{chats_count}`\n"
            f"• Пул `/г...` и `/g...`: `{'Активен 🟢' if has_global_pool(chat_data) else 'Не активен 🔴'}`\n"
            f"• Пул «Ники и роли»: `{'Активен 🟢' if has_nick_roles_pool(chat_data) else 'Не активен 🔴'}`"
        )

    elif cmd in ["info", "инфо"]:
        owner_fmt = await format_vk_user(int(owner_id)) if owner_id else "Не определён"
        antispam_str = "🟢 Вкл" if chat_data.get("antispam") else "🔴 Выкл"
        caps_str = "🟢 Вкл" if chat_data.get("caps_guard") else "🔴 Выкл"
        flood_str = "🟢 Вкл" if chat_data.get("antiflood") else "🔴 Выкл"
        lock_str = "🔒 Закрыт" if chat_data.get("locked") else "🔓 Открыт"
        return await message.answer(
            f"ℹ️ ═══ ИНФОРМАЦИЯ О БЕСЕДЕ ═══ ℹ️\n\n"
            f"• Беседа: **{chat_title}**\n"
            f"• Проект: **{proj_name}**\n"
            f"• Peer ID: `{peer_id}`\n"
            f"• Владелец: {owner_fmt}\n"
            f"• Режим чата: {lock_str}\n"
            f"• Защита: Спам {antispam_str} | Капс {caps_str} | Флуд {flood_str}\n"
            f"• Ваш уровень: `{user_lvl} lvl` ({get_role_title(user_lvl, chat_data, is_chat_owner(uid_int, chat_data, owner_id))})"
        )

    elif cmd in ["report", "репорт"]:
        if not params:
            return await message.answer("❌ Формат: `/репорт [описание проблемы, жалобы или вопроса]`")
        rep_text = " ".join(params).strip()
        rep_id = len(db["reports"]) + 1
        rep_entry = {
            "id": rep_id,
            "peer_id": peer_id,
            "chat_title": chat_title,
            "from_id": uid_int,
            "text": rep_text,
            "status": "open",
            "reply": None,
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        db["reports"].append(rep_entry)
        save_db(db)
        sender_fmt = await format_vk_user(uid_int)
        add_log(f"Репорт #{rep_id} в «{chat_title}» от {sender_fmt}: {rep_text}")
        if owner_id and int(owner_id) != uid_int:
            try:
                await bot.api.messages.send(
                    peer_id=int(owner_id),
                    message=(
                        f"📩 **НОВЫЙ РЕПОРТ #{rep_id}**\n"
                        f"• Беседа: **{chat_title}** (`{peer_id}`)\n"
                        f"• Отправитель: {sender_fmt}\n"
                        f"• Сообщение: {rep_text}\n\n"
                        f"💬 Ответить в чате: `/ответить {rep_id} [ваш ответ]`"
                    ),
                    random_id=0
                )
            except Exception:
                pass
        return await message.answer(
            f"✅ **Репорт #{rep_id} принят!**\n"
            f"• От: {sender_fmt}\n"
            f"• Текст: *{rep_text}*\n"
            f"• Статус: `Открыт` — ожидайте ответа администрации."
        )

    elif cmd in ["reports", "репорты"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        chat_reps = [r for r in db.get("reports", []) if r.get("peer_id") == peer_id][-8:]
        if not chat_reps:
            return await message.answer("📋 В этой беседе пока нет репортов.")
        lines = []
        for r in chat_reps:
            st_icon = "🟢 Закрыт" if r.get("status") == "closed" else "🟡 Открыт"
            u_fmt = await format_vk_user(r["from_id"])
            line = f"• **#{r['id']}** [{st_icon}] от {u_fmt}: {r['text']}"
            if r.get("reply"):
                line += f"\n   ↳ *Ответ:* {r['reply']}"
            lines.append(line)
        return await message.answer("📩 ═══ РЕПОРТЫ БЕСЕДЫ ═══ 📩\n\n" + "\n\n".join(lines))

    elif cmd in ["ответить", "answer"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        if len(params) < 2 or not params[0].lstrip("#").isdigit():
            return await message.answer("❌ Формат: `/ответить [номер репорта] [текст ответа]`")
        target_rep_id = int(params[0].lstrip("#"))
        ans_text = " ".join(params[1:]).strip()
        found_rep = next((r for r in db.get("reports", []) if r.get("id") == target_rep_id), None)
        if not found_rep:
            return await message.answer(f"❌ Репорт #{target_rep_id} не найден.")
        found_rep["reply"] = ans_text
        found_rep["status"] = "closed"
        save_db(db)
        add_log(f"Ответ на репорт #{target_rep_id} от {await format_vk_user(uid_int)}: {ans_text}")
        return await message.answer(
            f"✉️ **Ответ по репорту #{target_rep_id}** для {await format_vk_user(found_rep['from_id'])}:\n"
            f"• *Вопрос:* {found_rep['text']}\n"
            f"• *Ответ:* **{ans_text}**"
        )

    # =================================================================
    # ОТДЕЛЬНЫЙ ПУЛ «НИКИ И РОЛИ»
    # =================================================================
    elif cmd in ["ник", "nick"]:
        if not has_nick_roles_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer("🔒 Команда `/ник` входит в отдельный пул **«Ники и роли»**. Активируйте ключ через `/код [КЛЮЧ]`.")
        if not params:
            return await message.answer("❌ Формат: `/ник [Ваш никнейм]`")
        new_nick = " ".join(params).strip()
        if len(new_nick) > 40:
            return await message.answer("❌ Ник не длиннее 40 символов.")
        current_lvl = get_user_level_by_id(uid_int, chat_data, owner_id)
        if uid_str not in chat_data["members"] or not isinstance(chat_data["members"][uid_str], dict):
            chat_data["members"][uid_str] = {"level": current_lvl, "nickname": new_nick}
        else:
            chat_data["members"][uid_str]["nickname"] = new_nick
            chat_data["members"][uid_str]["level"] = current_lvl
        save_db(db)
        return await message.answer(f"✅ Ваш ник в беседе обновлён на: **{new_nick}**")

    elif cmd in ["nlist", "нлист"]:
        if not has_nick_roles_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer("🔒 Список ников `/нлист` входит в отдельный пул **«Ники и роли»**. Активируйте ключ через `/код [КЛЮЧ]`.")
        members = chat_data.get("members", {})
        nlist_lines = [
            f"• {await format_vk_user(int(m_id))} ➔ **{info['nickname']}**"
            for m_id, info in members.items()
            if isinstance(info, dict) and info.get("nickname", "").strip()
        ]
        if not nlist_lines:
            return await message.answer("📋 В этой беседе пока нет зарегистрированных ников.")
        return await message.answer("📋 ═══ АКТИВНЫЕ НИКНЕЙМЫ ═══ 📋\n\n" + "\n".join(nlist_lines))

    elif cmd in ["snick", "сник", "setnick", "сетник"]:
        if not has_nick_roles_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer("🔒 Выдача ников `/сник` входит в отдельный пул **«Ники и роли»**. Активируйте ключ через `/код [КЛЮЧ]`.")
        if user_lvl < 60:
            return await message.answer(f"❌ Недостаточно прав! Требуется 60+ уровень (у вас: {user_lvl}).")
        orig_parts = raw_text.split()[1:]
        if not orig_parts:
            return await message.answer("❌ Формат: `/сник [@ник/id/ответ] [Ник]`")
        target_id, remaining = await resolve_target(message, orig_parts)
        if not target_id:
            return await message.answer("❌ Пользователь не найден.")
        new_nick = " ".join(orig_parts).strip() if message.reply_message else " ".join(remaining).strip()
        if not new_nick:
            return await message.answer("❌ Укажите текст никнейма.")
        t_str = str(target_id)
        current_lvl = get_user_level_by_id(target_id, chat_data, owner_id)
        if t_str not in chat_data["members"] or not isinstance(chat_data["members"][t_str], dict):
            chat_data["members"][t_str] = {"level": current_lvl, "nickname": new_nick}
        else:
            chat_data["members"][t_str]["nickname"] = new_nick
            chat_data["members"][t_str]["level"] = current_lvl
        save_db(db)
        return await message.answer(f"✅ Ник **{new_nick}** выдан {await format_vk_user(target_id)}.")

    elif cmd in ["delnick", "делник"]:
        if not has_nick_roles_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer("🔒 Удаление ников `/делник` входит в отдельный пул **«Ники и роли»**. Активируйте ключ через `/код [КЛЮЧ]`.")
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        t_str = str(tid)
        if t_str in chat_data["members"] and isinstance(chat_data["members"][t_str], dict):
            chat_data["members"][t_str]["nickname"] = ""
            save_db(db)
            return await message.answer(f"🟢 Ник удалён у {await format_vk_user(tid)}.")
        return await message.answer("❌ Никнейм не найден в базе.")

    elif cmd in ["newrole", "новаяроль"]:
        if not has_nick_roles_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer("🔒 Создание ролей `/новаяроль` входит в отдельный пул **«Ники и роли»**. Активируйте ключ через `/код [КЛЮЧ]`.")
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        if len(params) < 2 or not params[0].isdigit():
            return await message.answer("❌ Формат: `/новаяроль [ур] [Название]`")
        lvl_key = str(int(params[0]))
        r_name = " ".join(params[1:])
        chat_data["role_names"][lvl_key] = r_name
        save_db(db)
        return await message.answer(f"✅ Роль для уровня `{lvl_key} lvl` установлена: **{r_name}**")

    elif cmd in ["delrole", "удалитьроль"]:
        if not has_nick_roles_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer("🔒 Удаление ролей `/удалитьроль` входит в отдельный пул **«Ники и роли»**. Активируйте ключ через `/код [КЛЮЧ]`.")
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        if not params or not params[0].isdigit():
            return await message.answer("❌ Укажите числовой уровень роли, например: `/удалитьроль 75`")
        lvl_key = str(int(params[0]))
        if lvl_key in chat_data["role_names"]:
            del chat_data["role_names"][lvl_key]
            save_db(db)
            return await message.answer(f"🗑 Роль уровня `{lvl_key}` удалена из списка беседы.")
        return await message.answer("❌ Указанная роль не найдена.")

    elif cmd in ["resetroles", "сбросролей"]:
        if not has_nick_roles_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer("🔒 Сброс ролей `/сбросролей` входит в отдельный пул **«Ники и роли»**. Активируйте ключ через `/код [КЛЮЧ]`.")
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        chat_data["role_names"] = {str(k): v for k, v in DEFAULT_ROLES.items()}
        save_db(db)
        return await message.answer("♻️ Список ролей беседы сброшен к стандартному.")

    elif cmd in ["setadmin", "сетадмин", "выдатьадм", "выдатьроль", "роль"]:
        if not has_nick_roles_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer("🔒 Выдача ролей `/сетадмин` входит в отдельный пул **«Ники и роли»**. Активируйте ключ через `/код [КЛЮЧ]`.")
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/сетадмин [@ник/id/ответ] [уровень]`")
        lvl_val = next((int(p) for p in rem if p.isdigit()), None)
        if lvl_val is None:
            return await message.answer("❌ Укажите числовой уровень админки, например: `/сетадмин @id1 70`")
        t_str = str(tid)
        current_data = chat_data["members"].get(t_str, {"nickname": ""})
        current_nick = current_data.get("nickname", "") if isinstance(current_data, dict) else ""
        chat_data["members"][t_str] = {"level": lvl_val, "nickname": current_nick}
        save_db(db)
        add_log(f"Выдача роли {lvl_val} lvl для {await format_vk_user(tid)} от {await format_vk_user(uid_int)}")
        return await message.answer(f"✅ {await format_vk_user(tid)} присвоен ранг `{lvl_val} lvl` (**{get_role_title(lvl_val, chat_data)}**).")

    elif cmd in ["removerole", "снятьроль", "снятадм"]:
        if not has_nick_roles_pool(chat_data) and uid_int != MAIN_OWNER_ID:
            return await message.answer("🔒 Снятие ролей `/снятьроль` входит в отдельный пул **«Ники и роли»**. Активируйте ключ через `/код [КЛЮЧ]`.")
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        t_str = str(tid)
        current_data = chat_data["members"].get(t_str, {"nickname": ""})
        current_nick = current_data.get("nickname", "") if isinstance(current_data, dict) else ""
        chat_data["members"][t_str] = {"level": 0, "nickname": current_nick}
        save_db(db)
        return await message.answer(f"🟢 С {await format_vk_user(tid)} сняты права админки (0 lvl).")

    # =================================================================
    # ВЫГОВОРЫ АДМИНАМ И МОДЕРАЦИЯ БЕЗ ПУЛА
    # =================================================================
    elif cmd in ["выговор", "reprimand", "vig"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/выговор [@ник/id/ответ] [причина]`")
        reason = " ".join(rem) if rem else "Нарушение должностного регламента"
        t_str = str(tid)
        chat_data["reprimands"].setdefault(t_str, []).append(reason)
        cnt = len(chat_data["reprimands"][t_str])
        u_fmt = await format_vk_user(tid)
        if cnt >= 3:
            current_data = chat_data["members"].get(t_str, {"nickname": ""})
            current_nick = current_data.get("nickname", "") if isinstance(current_data, dict) else ""
            chat_data["members"][t_str] = {"level": 0, "nickname": current_nick}
            chat_data["reprimands"][t_str] = []
            save_db(db)
            add_log(f"Снятие за 3 выговора: {u_fmt}")
            return await message.answer(f"🚨 {u_fmt} получил `3/3` выговора и был **автоматически снят с должности** (`0 lvl`)!")
        save_db(db)
        add_log(f"Выговор ({cnt}/3) для {u_fmt} от {await format_vk_user(uid_int)}: {reason}")
        return await message.answer(f"📕 {u_fmt} получил выговор (`{cnt}/3`). Причина: {reason}")

    elif cmd in ["выговоры", "reprimands", "vigs"]:
        tid, _ = await resolve_target(message, params)
        if not tid:
            tid = uid_int
        r_list = chat_data.get("reprimands", {}).get(str(tid), [])
        u_fmt = await format_vk_user(tid)
        if not r_list:
            return await message.answer(f"📋 У {u_fmt} нет выговоров (`0/3`).")
        reasons = "\n".join([f"{idx + 1}. {r}" for idx, r in enumerate(r_list)])
        return await message.answer(f"📕 **Выговоры {u_fmt} (`{len(r_list)}/3`):**\n\n{reasons}")

    elif cmd in ["снятьвыговор", "unreprimand", "unvig"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in chat_data["reprimands"] and chat_data["reprimands"][str(tid)]:
            chat_data["reprimands"][str(tid)].pop()
            save_db(db)
            return await message.answer(f"🟢 Последний выговор снят у {await format_vk_user(tid)}.")
        return await message.answer("❌ У пользователя нет активных выговоров.")

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
        target_id, _ = await resolve_target(message, params)
        if not target_id:
            target_id = uid_int
        t_str = str(target_id)
        owner_flag = is_chat_owner(target_id, chat_data, owner_id)
        target_lvl = 100 if owner_flag else get_user_level_by_id(target_id, chat_data, owner_id)
        r_name = get_role_title(target_lvl, chat_data, owner_flag=owner_flag)
        m_info = chat_data["members"].get(t_str, {})
        nick = m_info.get("nickname", "Не установлен") if isinstance(m_info, dict) and m_info.get("nickname") else "Не установлен"
        warns = len(chat_data.get("warns", {}).get(t_str, []))
        vigs = len(chat_data.get("reprimands", {}).get(t_str, []))
        rep_pts = int(chat_data.get("reputation", {}).get(t_str, 0))
        owner_badge = " 👑 **Владелец чата**" if owner_flag else ""
        return await message.answer(
            f"📊 ═══ ДОСЬЕ ИГРОКА ═══ 📊\n"
            f"👤 {await format_vk_user(target_id)}{owner_badge}\n\n"
            f"• Ник в чате: `{nick}`\n"
            f"• Должность: **{r_name}** `[{target_lvl} lvl]`\n"
            f"• Репутация: `{rep_pts}` ⭐\n"
            f"• Варны: `{warns}/3` | Выговоры: `{vigs}/3`"
        )

    elif cmd in ["check", "проверка"]:
        target_id, _ = await resolve_target(message, params)
        if not target_id:
            target_id = uid_int
        t_str = str(target_id)
        owner_flag = is_chat_owner(target_id, chat_data, owner_id)
        target_lvl = 100 if owner_flag else get_user_level_by_id(target_id, chat_data, owner_id)
        r_name = get_role_title(target_lvl, chat_data, owner_flag=owner_flag)
        m_info = chat_data["members"].get(t_str, {})
        nick = m_info.get("nickname", "Нет") if isinstance(m_info, dict) and m_info.get("nickname") else "Нет"
        leader_org = db.get("leaders", {}).get(t_str, "Нет")
        warns_list = chat_data.get("warns", {}).get(t_str, [])
        vigs_list = chat_data.get("reprimands", {}).get(t_str, [])
        rep_pts = int(chat_data.get("reputation", {}).get(t_str, 0))
        muted_until = chat_data.get("mutes", {}).get(t_str, 0)
        is_muted = time.time() < muted_until
        mute_str = f"Да (ещё {int((muted_until - time.time()) // 60) + 1} мин)" if is_muted else "Нет"
        is_banned = t_str in chat_data.get("bans", {}) or t_str in db.get("global_bans", {})
        ban_str = "Да 🔴" if is_banned else "Нет 🟢"
        return await message.answer(
            f"🔎 ═══ ПОЛНАЯ ПРОВЕРКА ═══ 🔎\n"
            f"👤 {await format_vk_user(target_id)}\n\n"
            f"• Владелец беседы: `{'Да 👑' if owner_flag else 'Нет'}`\n"
            f"• Уровень и роль: `{target_lvl} lvl` — **{r_name}**\n"
            f"• Ник: `{nick}` | Репутация: `{rep_pts}` ⭐\n"
            f"• Организация (лидер): `{leader_org}`\n"
            f"• Варны: `{len(warns_list)}/3` | Выговоры: `{len(vigs_list)}/3`\n"
            f"• Мут: `{mute_str}` | Бан: {ban_str}"
        )

    elif cmd in ["lock", "лок"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        chat_data["locked"] = True
        save_db(db)
        return await message.answer("🔒 Беседа закрыта для обычных участников (сообщения ниже 45 lvl удаляются).")

    elif cmd in ["unlock", "анлок"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        chat_data["locked"] = False
        save_db(db)
        return await message.answer("🔓 Беседа открыта для всех участников.")

    elif cmd in ["mute", "мут"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Ошибка доступа! Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите цель для мута (`@ник`, `id` или ответ).")
        mins = int(rem[0]) if rem and rem[0].isdigit() else 10
        chat_data["mutes"][str(tid)] = time.time() + (mins * 60)
        save_db(db)
        add_log(f"Мут: {await format_vk_user(tid)} на {mins} мин от {await format_vk_user(uid_int)}")
        return await message.answer(f"🤫 {await format_vk_user(tid)} замучен на `{mins}` мин.")

    elif cmd in ["unmuteall", "размутвсех"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        cnt = len(chat_data.get("mutes", {}))
        chat_data["mutes"] = {}
        save_db(db)
        return await message.answer(f"🔊 Сняты все активные муты в беседе (`{cnt}`).")

    elif cmd in ["unmute", "unmite", "анмут", "размут"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Недостаточно прав (нужен 45+, у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in chat_data["mutes"]:
            del chat_data["mutes"][str(tid)]
            save_db(db)
            return await message.answer(f"🔊 Мут снят с {await format_vk_user(tid)}.")
        return await message.answer("❌ Активных мутов у игрока не найдено.")

    elif cmd in ["ban", "бан"]:
        if user_lvl < 55:
            return await message.answer(f"❌ Требуется 55+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите цель для бана.")
        reason = " ".join(rem) if rem else "Нарушение регламента"
        chat_data["bans"][str(tid)] = {"reason": reason}
        save_db(db)
        try:
            await bot.api.messages.remove_chat_user(chat_id=message.chat_id, user_id=tid)
        except Exception:
            pass
        add_log(f"Бан в чате: {await format_vk_user(tid)}, причина: {reason}")
        return await message.answer(f"🚫 {await format_vk_user(tid)} заблокирован в чате. Причина: {reason}")

    elif cmd in ["unban", "анбан", "разбан"]:
        if user_lvl < 55:
            return await message.answer(f"❌ Требуется 55+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in chat_data["bans"]:
            del chat_data["bans"][str(tid)]
            save_db(db)
            return await message.answer(f"🟢 {await format_vk_user(tid)} разбанен в чате.")
        return await message.answer("❌ Этого игрока нет в бан-листе чата.")

    elif cmd in ["banlist", "банлист"]:
        bans = chat_data.get("bans", {})
        if not bans:
            return await message.answer("📋 Чёрный список беседы пуст.")
        lines = [f"• {await format_vk_user(int(b_id))} ➔ Причина: {info.get('reason')}" for b_id, info in bans.items()]
        return await message.answer("📋 ═══ ЧЁРНЫЙ СПИСОК БЕСЕДЫ ═══ 📋\n\n" + "\n".join(lines))

    elif cmd in ["chekban", "checkban", "чекбан"]:
        tid, _ = await resolve_target(message, params)
        if not tid:
            tid = uid_int
        is_b = str(tid) in chat_data.get("bans", {}) or str(tid) in db["global_bans"]
        status = "В бане 🔴" if is_b else "Чист 🟢"
        return await message.answer(f"🔍 Проверка статуса {await format_vk_user(tid)}: {status}")

    elif cmd in ["statschat", "статачата"]:
        c_count = len(db.get("global_chats", []))
        m_count = len(chat_data.get("members", {}))
        return await message.answer(f"📈 **СТАТИСТИКА БЕСЕДЫ:**\n• Участников в базе чата: `{m_count}`\n• Всего активных чатов: `{c_count}`")

    elif cmd in ["kick", "кик"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите цель для кика.")
        try:
            await bot.api.messages.remove_chat_user(chat_id=message.chat_id, user_id=tid)
            return await message.answer(f"✅ {await format_vk_user(tid)} исключён из беседы.")
        except Exception as e:
            return await message.answer(f"❌ Ошибка кика (проверьте права бота в беседе): {e}")

    elif cmd in ["warn", "варн"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите цель для варна.")
        reason = " ".join(rem) if rem else "Нарушение"
        t_str = str(tid)
        chat_data["warns"].setdefault(t_str, []).append(reason)
        save_db(db)
        cnt = len(chat_data["warns"][t_str])
        u_fmt = await format_vk_user(tid)
        await message.answer(f"⚠️ {u_fmt} получил варн (`{cnt}/3`). Причина: {reason}")
        if cnt >= 3:
            try:
                await bot.api.messages.remove_chat_user(chat_id=message.chat_id, user_id=tid)
                chat_data["warns"][t_str] = []
                save_db(db)
                await message.answer(f"🚫 Лимит исчерпан! {u_fmt} — авто-кик за 3 предупреждения.")
            except Exception:
                pass

    elif cmd in ["warns", "варны"]:
        tid, _ = await resolve_target(message, params)
        if not tid:
            tid = uid_int
        w_list = chat_data.get("warns", {}).get(str(tid), [])
        u_fmt = await format_vk_user(tid)
        if not w_list:
            return await message.answer(f"📋 У {u_fmt} нет предупреждений (`0/3`).")
        reasons = "\n".join([f"{idx + 1}. {r}" for idx, r in enumerate(w_list)])
        return await message.answer(f"⚠️ **Варны {u_fmt} (`{len(w_list)}/3`):**\n\n{reasons}")

    elif cmd in ["unwarn", "анварн", "снятварн"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in chat_data["warns"] and chat_data["warns"][str(tid)]:
            chat_data["warns"][str(tid)].pop()
            save_db(db)
            return await message.answer(f"🟢 Последний варн снят у {await format_vk_user(tid)}.")
        return await message.answer("❌ У игрока нет активных предупреждений.")

    elif cmd in ["clearwarns", "снятьвсеварны"]:
        if user_lvl < 55:
            return await message.answer(f"❌ Требуется 55+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        chat_data["warns"][str(tid)] = []
        save_db(db)
        return await message.answer(f"🟢 Все варны сняты у {await format_vk_user(tid)}.")

    elif cmd in ["antispam", "антиспам"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["antispam"] = not bool(chat_data.get("antispam"))
        save_db(db)
        state_txt = "🟢 ВКЛЮЧЁН (ссылки у участников <45 lvl удаляются)" if chat_data["antispam"] else "🔴 ВЫКЛЮЧЕН"
        return await message.answer(f"🛡 Режим антиспама в беседе: {state_txt}.")

    elif cmd in ["caps", "антикапс"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["caps_guard"] = not bool(chat_data.get("caps_guard"))
        save_db(db)
        state_txt = "🟢 ВКЛЮЧЁН" if chat_data["caps_guard"] else "🔴 ВЫКЛЮЧЕН"
        return await message.answer(f"🔠 Режим антикапса в беседе: {state_txt}.")

    elif cmd in ["antiflood", "антифлуд"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["antiflood"] = not bool(chat_data.get("antiflood"))
        save_db(db)
        state_txt = "🟢 ВКЛЮЧЁН" if chat_data["antiflood"] else "🔴 ВЫКЛЮЧЕН"
        return await message.answer(f"🌊 Режим антифлуда в беседе: {state_txt}.")

    elif cmd in ["clear", "очистить", "чистка"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        count = int(params[0]) if params and params[0].isdigit() else 10
        count = max(1, min(count, 50))
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
            return await message.answer("❌ Не удалось удалить сообщения (выдайте боту права администратора чата).")

    elif cmd in ["logs", "логи"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Доступно с 70 уровня (у вас: {user_lvl}).")
        recent = "\n".join(db["logs"][-15:])
        return await message.answer(f"📜 ═══ СИСТЕМНЫЕ ЛОГИ ═══ 📜\n\n{recent if recent else 'Журнал пуст'}")

    elif cmd in ["pin", "закреп"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Нужны права 45+ lvl (у вас: {user_lvl}).")
        if not message.reply_message:
            return await message.answer("❌ Сделайте реплай (ответ) на сообщение для закрепления.")
        try:
            await bot.api.messages.pin(peer_id=peer_id, conversation_message_id=message.reply_message.conversation_message_id)
            return await message.answer("📌 Сообщение закреплено!")
        except Exception:
            return await message.answer("❌ Ошибка закрепления: выдайте боту права администратора беседы в ВК.")

    elif cmd in ["unpin", "откреп"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Нужны права 45+ lvl (у вас: {user_lvl}).")
        try:
            await bot.api.messages.unpin(peer_id=peer_id)
            return await message.answer("📌 Закреплённое сообщение откреплено.")
        except Exception:
            return await message.answer("❌ Ошибка открепления: выдайте боту права администратора беседы в ВК.")

    elif cmd in ["setowner", "сетвладелец"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/сетвладелец [@ник/id/ответ]`")
        sync_owner_member(chat_data, int(tid))
        save_db(db)
        return await message.answer(f"👑 Владельцем чата закреплён {await format_vk_user(tid)} (`100 lvl`).")

    elif cmd in ["setleader", "сетлидер", "поставитьлидера"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid or not rem:
            return await message.answer("❌ Формат: `/сетлидер [@ник/id/ответ] [Организация]`")
        org = " ".join(rem)
        db["leaders"][str(tid)] = org
        save_db(db)
        return await message.answer(f"👔 {await format_vk_user(tid)} назначен лидером организации **{org}**.")

    elif cmd in ["removeleader", "снятлидера"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in db["leaders"]:
            del db["leaders"][str(tid)]
            save_db(db)
            return await message.answer(f"🟢 {await format_vk_user(tid)} снят с поста лидера.")
        return await message.answer("❌ Лидер не найден в списке.")

    elif cmd in ["leaders", "лидеры"]:
        if not db.get("leaders"):
            return await message.answer("👔 Список лидеров организаций пока пуст.")
        lines = [f"• {await format_vk_user(int(l_id))} ➔ **{org}**" for l_id, org in db["leaders"].items()]
        return await message.answer("👔 ═══ ЛИДЕРЫ ОРГАНИЗАЦИЙ ═══ 👔\n\n" + "\n".join(lines))

    # =================================================================
    # СЕТЕВЫЕ КОМАНДЫ НА /г... И /g... (ТОЛЬКО ПО АКТИВИРОВАННОМУ ПУЛУ)
    # =================================================================
    elif cmd in ["grole", "гроль"]:
        if user_lvl < 95:
            return await message.answer(f"❌ Требуется 95+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid or not rem or not rem[0].isdigit():
            return await message.answer("❌ Формат: `/гроль [@ник/id] [уровень]`")
        db["global_roles"][str(tid)] = int(rem[0])
        save_db(db)
        return await message.answer(f"🌐 Глобальная роль `{rem[0]}` выдана {await format_vk_user(tid)} по всей сети.")

    elif cmd in ["grr", "снятьгроль"]:
        if user_lvl < 95:
            return await message.answer(f"❌ Требуется 95+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in db["global_roles"]:
            del db["global_roles"][str(tid)]
            save_db(db)
            return await message.answer(f"🟢 Глобальная роль снята у {await format_vk_user(tid)}.")
        return await message.answer("❌ У игрока нет глобальных ролей.")

    elif cmd in ["gban", "гбан", "гбн"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите цель.")
        reason = " ".join(rem) if rem else "Глобальное нарушение"
        db["global_bans"][str(tid)] = reason
        save_db(db)
        add_log(f"Глобальный бан: {await format_vk_user(tid)}, причина: {reason}")
        return await message.answer(f"⛔ ГЛОБАЛЬНЫЙ БАН для {await format_vk_user(tid)}! Причина: {reason}")

    elif cmd in ["gunban", "гунбан", "гразбан"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in db["global_bans"]:
            del db["global_bans"][str(tid)]
            save_db(db)
            return await message.answer(f"🟢 {await format_vk_user(tid)} убран из глобального бана.")
        return await message.answer("❌ Пользователя нет в глобальном бане.")

    elif cmd in ["gbanlist", "гбанлист"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        gbans = db.get("global_bans", {})
        if not gbans:
            return await message.answer("🌍 Глобальный чёрный список пуст.")
        lines = [f"• {await format_vk_user(int(b_id))} ➔ {rsn}" for b_id, rsn in list(gbans.items())[-25:]]
        return await message.answer("⛔ ═══ ГЛОБАЛЬНЫЙ БАН-ЛИСТ СЕТИ ═══ ⛔\n\n" + "\n".join(lines))

    elif cmd in ["gcheckban", "гчекбан"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            tid = uid_int
        rsn = db.get("global_bans", {}).get(str(tid))
        return await message.answer(
            f"🌍 Проверка глобального ЧС для {await format_vk_user(tid)}: "
            + (f"⛔ В глобальном бане ({rsn})" if rsn else "🟢 Чист")
        )

    elif cmd in ["gcheck", "гпроверка"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            tid = uid_int
        t_str = str(tid)
        g_lvl = db.get("global_roles", {}).get(t_str, 0)
        g_ban = db.get("global_bans", {}).get(t_str)
        ldr = db.get("leaders", {}).get(t_str, "Нет")
        return await message.answer(
            f"🌍 ═══ СЕТЕВОЕ ДОСЬЕ ═══ 🌍\n"
            f"👤 {await format_vk_user(tid)}\n\n"
            f"• Глобальный уровень: `{g_lvl} lvl`\n"
            f"• Глобальный бан: `{'Да — ' + g_ban if g_ban else 'Нет 🟢'}`\n"
            f"• Лидерство в сети: `{ldr}`"
        )

    elif cmd in ["gtop", "гтоп"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        totals = {}
        for c_info in db.get("chats", {}).values():
            for u_id, val in c_info.get("reputation", {}).items():
                totals[u_id] = totals.get(u_id, 0) + int(val)
        if not totals:
            return await message.answer("🌍 Сетевой рейтинг репутации пока пуст.")
        top_net = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:10]
        lines = [f"{i + 1}. {await format_vk_user(int(u))} — **`{v}`** ⭐" for i, (u, v) in enumerate(top_net)]
        return await message.answer("🌍 ═══ ТОП РЕПУТАЦИИ ПО ВСЕЙ СЕТИ ═══ 🌍\n\n" + "\n".join(lines))

    elif cmd in ["gstats", "гстата", "gchats", "гчаты"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        return await message.answer(
            f"🌍 ═══ СЕТЕВАЯ СТАТИСТИКА ПУЛА ═══ 🌍\n\n"
            f"• Всего бесед бота: `{len(db.get('global_chats', []))}`\n"
            f"• Активных бесед в пуле: `{count_active_pool_chats()}/{MAX_ACTIVE_POOL_CHATS}`\n"
            f"• Глобальных ролей: `{len(db.get('global_roles', {}))}`\n"
            f"• Глобальных банов: `{len(db.get('global_bans', {}))}`\n"
            f"• Лидеров в сети: `{len(db.get('leaders', {}))}`"
        )

    elif cmd in ["admins", "админс"]:
        lines = []
        shown_ids = set()

        if owner_id:
            owner_int = int(owner_id)
            shown_ids.add(owner_int)
            o_info = chat_data["members"].get(str(owner_int))
            o_nick = f" — `{o_info['nickname']}`" if isinstance(o_info, dict) and o_info.get("nickname") else ""
            lines.append(f"👑 **Владелец чата:** {await format_vk_user(owner_int)}{o_nick} ➔ `100 lvl`")

        for m_id, info in chat_data.get("members", {}).items():
            uid_item = int(m_id)
            if uid_item in shown_ids:
                continue
            lvl = info.get("level", 0) if isinstance(info, dict) else int(info or 0)
            if lvl > 0:
                shown_ids.add(uid_item)
                r_name = get_role_title(lvl, chat_data)
                nick_str = f" — `{info.get('nickname')}`" if isinstance(info, dict) and info.get("nickname") else ""
                lines.append(f"• {await format_vk_user(uid_item)}{nick_str} ➔ **{r_name}** `[{lvl} lvl]`")

        for g_id, lvl in db.get("global_roles", {}).items():
            uid_item = int(g_id)
            if uid_item in shown_ids or int(lvl) <= 0:
                continue
            shown_ids.add(uid_item)
            r_name = get_role_title(int(lvl), chat_data)
            lines.append(f"• 🌐 {await format_vk_user(uid_item)} ➔ **{r_name}** `[{lvl} lvl]`")

        if not lines:
            return await message.answer("👑 В этом чате пока не определён владелец и нет назначенных администраторов.")

        return await message.answer("👑 ═══ АДМИНИСТРАЦИЯ БЕСЕДЫ ═══ 👑\n\n" + "\n".join(lines), keyboard=get_chat_inline_keyboard())

    elif cmd in ["staff", "состав"]:
        parts = []
        if owner_id:
            parts.append(f"👑 **Владелец чата:** {await format_vk_user(int(owner_id))} (`100 lvl`)")

        adm_lines = []
        for m_id, info in chat_data.get("members", {}).items():
            lvl = info.get("level", 0) if isinstance(info, dict) else int(info or 0)
            if lvl > 0 and int(m_id) != int(owner_id or 0):
                adm_lines.append(f"• {await format_vk_user(int(m_id))} — **{get_role_title(lvl, chat_data)}** `[{lvl} lvl]`")
        if adm_lines:
            parts.append("🛡 **Администраторы:**\n" + "\n".join(adm_lines))

        if db.get("leaders"):
            lead_lines = [f"• {await format_vk_user(int(l_id))} — **{org}**" for l_id, org in db["leaders"].items()]
            parts.append("👔 **Лидеры:**\n" + "\n".join(lead_lines))

        if not parts:
            return await message.answer("📋 Состав беседы пока пуст.")
        return await message.answer("📋 ═══ СОСТАВ БЕСЕДЫ ═══ 📋\n\n" + "\n\n".join(parts))


# =====================================================================
# 8. ЗАПУСК БОТА
# =====================================================================
if __name__ == "__main__":
    bot.run_forever()
