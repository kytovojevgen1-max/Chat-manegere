import asyncio
import json
import os
import secrets
import time
from aiohttp import web
from vkbottle import Bot, Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Message

# =====================================================================
# 1. КОНФИГУРАЦИЯ И ИНИЦИАЛИЗАЦИЯ
# =====================================================================
TOKEN = os.getenv(
    "VK_TOKEN",
    "vk1.a.ONWFvnPZaVTXfIN1NsuwhyGfU6LsNurMI8qT_pVrqm_pxrdIBRvh_ibzoBVUmX5DhOTv26MSpMPmeKEwCZvtIbjfCByslMQ7_-K7o8ipviBYTbslgWeMHoDeWZ5HXtBVPNNCGfkXdK0DsjOe71hXQBXPvfI6K1h3S4YfrPAd6k6I1u8v4Nmp5tZSmZkgISWrhX7WtIcbWkteWFeFXrUd-g",
)
MAIN_OWNER_ID = 897082738
DB_FILE = "bot_database.json"
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

bot = Bot(token=TOKEN)


# =====================================================================
# 2. БАЗА ДАННЫХ И СОСТОЯНИЯ
# =====================================================================
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
        "broadcast_admins": [],
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
    tmp_file = f"{DB_FILE}.tmp"
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, DB_FILE)
    except Exception as e:
        print(f"[DB SAVE ERROR] {e}")


db = load_db()
user_states: dict[int, str] = {}
user_cache: dict[int, dict] = {}
flood_tracker: dict[tuple[int, int], list[float]] = {}
rep_cooldown: dict[tuple[int, int], float] = {}
pint_cooldown: dict[tuple[int, int], float] = {}


def add_log(entry: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    db["logs"].append(f"[{stamp}] {entry}")
    db["logs"] = db["logs"][-200:]
    save_db(db)


def get_chat_id_from_peer(message: Message) -> int | None:
    cid = getattr(message, "chat_id", None)
    if isinstance(cid, int) and cid > 0:
        return cid
    if message.peer_id > 2000000000:
        return message.peer_id - 2000000000
    return None


def get_chat_data(peer_id: int) -> dict:
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
    }
    if pid not in db["chats"] or not isinstance(db["chats"][pid], dict):
        db["chats"][pid] = default_chat
        return db["chats"][pid]

    chat = db["chats"][pid]
    for k, v in default_chat.items():
        if k not in chat:
            chat[k] = v
    if not isinstance(chat.get("role_names"), dict) or not chat["role_names"]:
        chat["role_names"] = {str(k): v for k, v in DEFAULT_ROLES.items()}
    return chat


# =====================================================================
# 3. СИСТЕМА РАНГОВ И ВЫДАЧИ АДМИНОК
# =====================================================================
def set_member_level(chat_data: dict, uid: int, level: int, nickname: str | None = None) -> None:
    u_str = str(int(uid))
    cur = chat_data.setdefault("members", {}).get(u_str, {})
    cur_nick = cur.get("nickname", "") if isinstance(cur, dict) else ""
    final_nick = cur_nick if nickname is None else nickname
    chat_data["members"][u_str] = {
        "level": max(0, int(level)),
        "nickname": final_nick,
    }


def get_member_level_raw(chat_data: dict, uid: int) -> int:
    m_info = chat_data.get("members", {}).get(str(int(uid)))
    if isinstance(m_info, dict):
        return int(m_info.get("level", 0) or 0)
    if isinstance(m_info, (int, float, str)) and str(m_info).isdigit():
        return int(m_info)
    return 0


def is_chat_owner(uid: int, chat_data: dict, owner_id: int | None) -> bool:
    if MAIN_OWNER_ID and uid == MAIN_OWNER_ID:
        return True
    if owner_id is not None and uid == int(owner_id):
        return True
    if chat_data.get("owner_id") is not None and uid == int(chat_data["owner_id"]):
        return True
    return False


def sync_owner_member(chat_data: dict, owner_uid: int) -> None:
    chat_data["owner_id"] = int(owner_uid)
    set_member_level(chat_data, int(owner_uid), 100)


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
    if MAIN_OWNER_ID and uid == MAIN_OWNER_ID:
        return 100
    if is_chat_owner(uid, chat_data, owner_id):
        return 100
    local_lvl = get_member_level_raw(chat_data, uid)
    global_lvl = int(db.get("global_roles", {}).get(str(int(uid)), 0) or 0)
    return max(local_lvl, global_lvl)


# =====================================================================
# 4. ОПРЕДЕЛЕНИЕ ВЛАДЕЛЬЦА БЕСЕДЫ, АВТО-ВЫДАЧА 100 LVL И РАССЫЛКА
# =====================================================================
async def get_vk_chat_member_flags(peer_id: int, uid: int) -> tuple[bool, bool]:
    try:
        members_res = await bot.api.messages.get_conversation_members(peer_id=peer_id)
        for item in (members_res.items or []):
            if int(item.member_id) == int(uid):
                return bool(getattr(item, "is_owner", False)), bool(getattr(item, "is_admin", False))
    except Exception:
        pass
    return False, False


async def find_star_holder_in_chat(peer_id: int) -> int | None:
    try:
        members_res = await bot.api.messages.get_conversation_members(peer_id=peer_id)
        for item in (members_res.items or []):
            mid = int(getattr(item, "member_id", 0) or 0)
            if mid > 0 and (bool(getattr(item, "is_owner", False)) or bool(getattr(item, "is_admin", False))):
                return mid
    except Exception:
        pass
    return None


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
        star_uid = await find_star_holder_in_chat(peer_id)
        if star_uid:
            owner_id = star_uid
            sync_owner_member(chat_data, star_uid)

    return chat_title, int(owner_id) if owner_id else None


async def auto_grant_star_100(peer_id: int, uid: int, chat_data: dict, owner_id: int | None) -> int | None:
    if uid > 0:
        is_vk_owner, is_vk_admin = await get_vk_chat_member_flags(peer_id, uid)
        if is_vk_owner or is_vk_admin:
            if chat_data.get("owner_id") is None:
                sync_owner_member(chat_data, int(uid))
                save_db(db)
                add_log(f"Авто-выдан 100 lvl (звезда ВК) ID {uid} в чате {peer_id}")
                return int(uid)

    if owner_id is None:
        star_uid = await find_star_holder_in_chat(peer_id)
        if star_uid:
            sync_owner_member(chat_data, int(star_uid))
            save_db(db)
            add_log(f"Авто-выдан 100 lvl создателю/админу со звездой ID {star_uid} в чате {peer_id}")
            return int(star_uid)

    return owner_id


def build_welcome_guide(chat_data: dict) -> str:
    proj_name = chat_data.get("project_name", DEFAULT_PROJECT_NAME)
    return (
        f"👋 ═══ {proj_name} — БОТ ПОДКЛЮЧЁН ═══ 👋\n\n"
        "• Команды и ранги: `/help`\n"
        "• Игры и развлечения (в т.ч. Пинтибот): `/игры`\n"
        "• Интерактивная панель: `/menu` или `/панель`\n"
        "• Создателю или админу со звездой `100 lvl` выдаётся автоматически или по `/явладелец`."
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
    for cid in list(db.get("global_chats", [])):
        try:
            await bot.api.messages.send(peer_id=int(cid), message=broadcast_text, random_id=0)
            count += 1
        except Exception:
            pass
    add_log(f"Массовая рассылка выполнена {sender_fmt}. Охвачено чатов: {count}")
    return count


# =====================================================================
# 5. ПОМОЩНИКИ ВК, КЛАВИАТУРЫ И ИГРЫ (ПИНТИБОТ + НАБОР ИГР)
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


async def get_chat_user_ids(peer_id: int, chat_data: dict, fallback_uid: int) -> list[int]:
    try:
        members_res = await bot.api.messages.get_conversation_members(peer_id=peer_id)
        ids = [int(m.member_id) for m in (members_res.items or []) if int(m.member_id) > 0]
        if ids:
            return ids
    except Exception:
        pass
    ids = [int(k) for k in chat_data.get("members", {}).keys() if k.lstrip("-").isdigit() and int(k) > 0]
    return ids if ids else [fallback_uid]


PINTIBOT_SCENARIOS = [
    ("🍺 Пинтибот налил свежего пива с густой пенкой — настроения +", "🍺"),
    ("🍻 Пинтибот зашёл в бар, опрокинул кружку и выдал бодрость — заряд +", "🍻"),
    ("🥂 Пинтибот поднял тост за весь чат — удача выросла на +", "🥂"),
    ("🥃 Пинтибот устроил ночной челлендж на выдержку — счётчик +", "🥃"),
    ("🍻 Пинтибот открыл крафтовый кег прямо посреди чата — хайп +", "🍻"),
]


async def run_pintibot(message: Message, uid_int: int, peer_id: int, chat_data: dict, params: list[str]) -> str:
    cd_key = (peer_id, uid_int)
    now_t = time.time()
    if uid_int != MAIN_OWNER_ID and now_t - pint_cooldown.get(cd_key, 0) < 10:
        left = int(10 - (now_t - pint_cooldown[cd_key])) + 1
        return f"⏳ Пинтибот перезаряжает кружку, подожди `{left}` сек."
    pint_cooldown[cd_key] = now_t

    tid, _ = await resolve_target(message, params)
    target_id = tid if (tid and tid > 0) else uid_int
    u_fmt = await format_vk_user(uid_int)
    t_fmt = await format_vk_user(target_id)

    delta = secrets.randbelow(25) + 1
    phrase, emoji = secrets.choice(PINTIBOT_SCENARIOS)
    t_str = str(target_id)
    cur_rep = int(chat_data.get("reputation", {}).get(t_str, 0))
    new_rep = cur_rep + max(1, delta // 5)
    chat_data["reputation"][t_str] = new_rep
    save_db(db)

    if target_id != uid_int:
        return (
            f"{emoji} **ПИНТИБОТ** {emoji}\n\n"
            f"• {u_fmt} угощает {t_fmt}!\n"
            f"• {phrase}**`{delta}`**\n"
            f"• Репутация {t_fmt}: **`{new_rep}`** ⭐"
        )
    return (
        f"{emoji} **ПИНТИБОТ** {emoji}\n\n"
        f"• Игрок: {u_fmt}\n"
        f"• {phrase}**`{delta}`**\n"
        f"• Твоя репутация: **`{new_rep}`** ⭐"
    )


def get_chat_inline_keyboard() -> str:
    kb = Keyboard(one_time=False, inline=True)
    kb.add(Text("👑 Админы"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("📊 Стата"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("🎮 Игры"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("🍺 Пинтибот"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("📋 Роли"), color=KeyboardButtonColor.POSITIVE)
    kb.add(Text("📜 Правила"), color=KeyboardButtonColor.PRIMARY)
    kb.row()
    kb.add(Text("🛠 Модерация"), color=KeyboardButtonColor.NEGATIVE)
    kb.add(Text("📖 Команды"), color=KeyboardButtonColor.SECONDARY)
    return kb.get_json()


def build_games_help() -> str:
    return (
        "🎮 ═══ ИГРЫ И РАЗВЛЕЧЕНИЯ ═══ 🎮\n\n"
        "• `/пинтибот [@ник]` (`/пинтбот`, `/pintbot`) — фирменный Пинтибот\n"
        "• `/куб` (`/кости`, `/dice`) — бросить кубик `1–6`\n"
        "• `/монетка` (`/coin`) — орёл или решка\n"
        "• `/рулетка` (`/roulette`) — русская рулетка\n"
        "• `/дуэль @ник` (`/duel`) — дуэль с участником\n"
        "• `/слоты` (`/slots`) — игровой автомат\n"
        "• `/камень [к/н/б]` (`/кнб`) — камень, ножницы, бумага\n"
        "• `/угадай [1-10]` — угадать число бота\n"
        "• `/процент [текст]` / `/шанс [текст]` — измеритель вероятности\n"
        "• `/кто [вопрос]` — случайный участник чата\n"
        "• `/выбор [1 / 2]` — выбор из вариантов\n"
        "• `+реп @ник` / `/топреп` — репутация в беседе"
    )


def build_full_help_with_ranks(proj_name: str) -> str:
    return (
        f"📖 ═══ ПОЛНЫЙ СПРАВОЧНИК И РАНГИ ({proj_name}) ═══ 📖\n\n"
        "💡 *Цель указывается через `@ник`, `id`, ссылку ВК или реплай.*\n"
        "📌 *Разделы:* `/help мод` | `/help роли` | `/help игры` | `/help сеть`\n\n"
        "🌐 **0 lvl — Все участники:**\n"
        "• `/menu` (`/панель`) — интерактивные кнопки в беседе\n"
        "• `/игры` (`/games`) — список всех игр (включая Пинтибот)\n"
        "• `/пинтибот [@ник]` (`/пинтбот`) — мини-игра Пинтибот\n"
        "• `/стата` (`/stats`, `/профиль`) — досье игрока\n"
        "• `/проверка` (`/check`) — расширенная проверка статуса, варнов, выговоров и банов\n"
        "• `/админс` (`/admins`) — владелец (`100 lvl`) и администраторы чата\n"
        "• `/состав` (`/staff`) — полный состав: владелец, админы и лидеры\n"
        "• `/роли` (`/roles`) — иерархия рангов беседы\n"
        "• `/правила` (`/rules`) — регламент беседы\n"
        "• `/лидеры` (`/leaders`) — список лидеров организаций\n"
        "• `/инфо` (`/info`) — информация и настройки беседы\n"
        "• `/репорт [текст]` (`/report`) — отправить обращение администрации\n"
        "• `/заметки` — список заметок беседы\n"
        "• `/реп @цель` (или `+реп @цель`) / `/топреп` — репутация в беседе\n"
        "• `/кто`, `/шанс`, `/выбор`, `/куб`, `/монетка`, `/рулетка`, `/дуэль`, `/слоты`, `/камень`, `/угадай`, `/процент`\n"
        "• `/мойид`, `/время`, `/пинг`, `/статус` — системные утилиты\n"
        "• `/явладелец` — закрепить владельца (`100 lvl`) *(только создатель или админ со звездой)*\n\n"
        "🌱 **15+ lvl — Младший состав:**\n"
        "• `/заметка [имя] | [текст]` — добавить заметку\n"
        "• `/удзаметку [имя]` — удалить заметку\n\n"
        "🛡 **45+ lvl — Модерация чата:**\n"
        "• `/мут @цель [мин]` — выдать мут (по умолчанию 10 мин)\n"
        "• `/анмут @цель` — снять мут\n"
        "• `/кик @цель` — исключить участника из беседы\n"
        "• `/варн @цель [причина]` — выдать варн (`3/3` — авто-кик)\n"
        "• `/варны @цель` / `/анварн @цель` — просмотр и снятие последнего варна\n"
        "• `/очистить [1-100]` — удалить последние сообщения\n"
        "• `/закреп` (реплай) / `/откреп` — закрепить или открепить сообщение\n"
        "• `/лок` / `/анлок` — закрыть или открыть чат для участников `<45 lvl`\n\n"
        "🚨 **55+ lvl — Старшая модерация:**\n"
        "• `/бан @цель [причина]` — заблокировать в чате (и при повторном входе)\n"
        "• `/анбан @цель` / `/банлист` / `/чекбан @цель` — управление локальным ЧС\n"
        "• `/снятьвсеварны @цель` — обнулить все варны игрока\n"
        "• `/статачата` — статистика участников и подключённых чатов\n\n"
        "👑 **60+ lvl — Руководство:**\n"
        "• `/ник [Ник]` (`0+`) / `/нлист` (`0+`) / `/сник @цель [Ник]` / `/делник @цель` — ники в беседе\n"
        "• `/сетлидер @цель [Орг]` / `/снятлидера @цель` — назначение и снятие лидеров\n"
        "• `/репорты` / `/ответить [#id] [ответ]` — просмотр и закрытие тикетов\n"
        "• `/созыв [текст]` — созвать участников (`@all`)\n\n"
        "⚡ **70+ lvl — Старшая администрация и защита:**\n"
        "• `/сетадмин @цель [ур]` / `/снятьроль @цель` — выдача и снятие ранга\n"
        "• `/повысить @цель [ур/шаг]` / `/понизить @цель [ур/шаг]` — гибкая смена ранга\n"
        "• `/выговор @цель [причина]` (`3/3` — авто-снятие) / `/выговоры` / `/снятьвыговор` / `/снятьвсевыговоры`\n"
        "• `/сетправила [текст]` / `/сбросправил` — редактирование правил беседы\n"
        "• `/сетпривет [текст]` / `/сброспривета` — приветствие при входе (`{user}`, `{chat}`)\n"
        "• `/размутвсех` — снять все муты в беседе\n"
        "• `/антиспам` / `/антикапс` / `/антифлуд` / `/антиреклама` — переключатели авто-защиты\n"
        "• `/логи` — журнал действий и событий\n\n"
        "🔥 **90+ lvl — Высший состав и кастомизация:**\n"
        "• `/новаяроль [ур] [Название]` / `/удалитьроль [ур]` / `/сбросролей` — настройка ролей\n"
        "• `/сетназвание [Название]` / `/сетссылка [URL]` — брендинг конкретной беседы\n"
        "• `/сетвладелец @цель` / `/сбросвладельца` — управление владельцем чата\n\n"
        "🌍 **Сетевые команды `/г...` и `/g...`:**\n"
        "• `/гбан` (`90+`), `/гунбан` (`90+`), `/гбанлист` (`60+`), `/гчекбан` (`45+`)\n"
        "• `/гмут` (`60+`), `/гранмут` (`60+`), `/гкик` (`60+`), `/гварн` (`60+`)\n"
        "• `/гроль` (`95+`), `/снятьгроль` (`95+`), `/гвыговор` (`90+`), `/гвыговоры` (`60+`), `/гснятьвыговор` (`90+`)\n"
        "• `/гсозыв` (`70+`), `/грассылка [текст]` *(владелец бота / 95+)*\n"
        "• `/гпроверка` (`60+`), `/гстата` (`60+`), `/гчаты` (`60+`), `/гтоп` (`45+`)"
    )


# =====================================================================
# 6. ЛИЧНЫЕ СООБЩЕНИЯ (ПАНЕЛЬ УПРАВЛЕНИЯ И РАССЫЛКА)
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

    return kb.get_json()


@bot.on.private_message()
async def private_handler(message: Message):
    uid = message.from_id
    text = (message.text or "").strip()
    if not text:
        return

    low = text.lower()
    state = user_states.get(uid)
    is_main = (uid == MAIN_OWNER_ID)
    has_broadcast = (is_main or uid in db["broadcast_admins"])

    if low in ["/start", "начать", "меню", "🔙 главное меню", "❌ отмена"]:
        user_states.pop(uid, None)
        await message.answer(
            f"👑 ═══ {DEFAULT_PROJECT_NAME} — ПАНЕЛЬ УПРАВЛЕНИЯ ═══ 👑\n\n"
            "• Бот работает во всех подключённых беседах без ключей и пулов.\n"
            "• В беседах доступны модерация, роли, ники, репутация и игры (включая Пинтибот).",
            keyboard=get_private_keyboard(uid)
        )
        return

    if text in ["➕ Админ рассылки", "➕ Выдать доступ к рассылке"]:
        if not has_broadcast:
            return
        user_states[uid] = "wait_add_adm"
        await message.answer(
            "✍️ Введите цифровой VK ID или @ник пользователя для выдачи доступа к глобальной рассылке:",
            keyboard=Keyboard(one_time=False, inline=True).add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE).get_json()
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
            keyboard=get_private_keyboard(uid)
        )
        return

    if text in ["📢 Рассылка по чатам", "📢 Начать рассылку по чатам"]:
        if not has_broadcast:
            return
        user_states[uid] = "wait_broadcast"
        await message.answer(
            "✍️ Введите текст для массовой отправки по всем подключённым беседам:",
            keyboard=Keyboard(one_time=False, inline=True).add(Text("❌ Отмена"), color=KeyboardButtonColor.NEGATIVE).get_json()
        )
        return

    if text in ["🌐 Ссылка проекта", "🌐 Сайт проекта"]:
        await message.answer(f"🌐 Ссылка проекта: {DEFAULT_PROJECT_URL}", keyboard=get_private_keyboard(uid))
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
        "👋 Используй кнопки ниже для управления рассылками и просмотра ссылки проекта.",
        keyboard=get_private_keyboard(uid)
    )


# =====================================================================
# 7. РАБОТА В БЕСЕДАХ
# =====================================================================
@bot.on.chat_message()
async def chat_handler(message: Message):
    peer_id = message.peer_id
    uid_int = message.from_id
    uid_str = str(uid_int)
    chat_id_num = get_chat_id_from_peer(message)
    chat_data = get_chat_data(peer_id)

    is_new_chat = peer_id not in db["global_chats"]
    if is_new_chat:
        db["global_chats"].append(peer_id)

    chat_title, owner_id = await resolve_chat_owner(peer_id, chat_data)
    owner_id = await auto_grant_star_100(peer_id, uid_int, chat_data, owner_id)
    save_db(db)

    # --- Событие входа / приглашения ---
    if message.action and message.action.type == "chat_invite_user":
        j_id = message.action.member_id
        group_id = getattr(message, "group_id", None)
        is_bot_invited = (group_id and j_id == -group_id) or (j_id is not None and j_id < 0)

        if is_bot_invited or (is_new_chat and not chat_data.get("welcomed")):
            chat_data["welcomed"] = True
            star_uid = await find_star_holder_in_chat(peer_id)
            if star_uid:
                sync_owner_member(chat_data, int(star_uid))
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

    # --- Антиреклама ---
    if chat_data.get("antiads") and user_lvl < 45:
        if any(tag in clean_text for tag in ["[club", "[public", "vk.com/join", "vk.me/", "t.me/"]):
            try:
                await bot.api.messages.delete(
                    peer_id=peer_id,
                    conversation_message_ids=[message.conversation_message_id],
                    delete_for_all=True
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

    if clean_text.startswith("+реп"):
        rem_part = raw_text[4:].strip().split()
        raw_text = ("/реп " + " ".join(rem_part)).strip()
        clean_text = raw_text.lower()

    # --- Кнопки и быстрые фразы ---
    if clean_text in ["/menu", "/panel", "/панель", "меню", "панель"]:
        return await message.answer(f"👑 ═══ ПАНЕЛЬ УПРАВЛЕНИЯ — {proj_name} ═══ 👑", keyboard=get_chat_inline_keyboard())

    if clean_text in ["🎮 игры", "игры", "/игры", "/games", "игровая", "развлечения"]:
        return await message.answer(build_games_help(), keyboard=get_chat_inline_keyboard())

    if clean_text in ["🍺 пинтибот", "пинтибот", "пинтбот"]:
        res_txt = await run_pintibot(message, uid_int, peer_id, chat_data, [])
        return await message.answer(res_txt, keyboard=get_chat_inline_keyboard())

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
            "• `60+ lvl`: `/ник`, `/нлист`, `/сник`, `/делник`, `/сетлидер`, `/снятлидера`, `/репорты`, `/ответить`, `/созыв`\n"
            "• `70+ lvl`: `/сетадмин`, `/снятьроль`, `/повысить`, `/понизить`, `/выговор`, `/выговоры`, `/снятьвыговор`, `/сетправила`, `/сетпривет`, `/размутвсех`, `/антиспам`, `/антикапс`, `/антифлуд`, `/антиреклама`, `/логи`\n"
            "• `90+ lvl`: `/новаяроль`, `/удалитьроль`, `/сбросролей`, `/сетназвание`, `/сетссылка`, `/сетвладелец`, `/сбросвладельца`\n"
            "• `Сеть /г... и /g...`: `/гбан`, `/гунбан`, `/гбанлист`, `/гчекбан`, `/гмут`, `/гранмут`, `/гкик`, `/гварн`, `/гроль`, `/снятьгроль`, `/гвыговор`, `/гвыговоры`, `/гснятьвыговор`, `/гсозыв`, `/грассылка`, `/гпроверка`, `/гстата`, `/гчаты`, `/гтоп`",
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
    # УМНЫЙ /HELP С РАНГАМИ И РАЗДЕЛАМИ
    # =================================================================
    if cmd in ["help", "хелп", "cmds", "команды"]:
        section = params[0].lower() if params else ""

        if section in ["игры", "game", "games", "фан", "fun"]:
            return await message.answer(build_games_help(), keyboard=get_chat_inline_keyboard())

        if section in ["мод", "mod", "модерация"]:
            return await message.answer(
                "🛡 ═══ МОДЕРАЦИЯ И ЗАЩИТА (ПО РАНГАМ) ═══ 🛡\n\n"
                "• `/мут @цель [мин]` (`45+ lvl`) — мут с авто-удалением сообщений\n"
                "• `/анмут @цель` (`45+ lvl`) / `/размутвсех` (`70+ lvl`) — снятие мута\n"
                "• `/варн @цель [причина]` (`45+ lvl`) — предупреждение (`3/3` = авто-кик)\n"
                "• `/варны @цель` / `/анварн @цель` (`45+ lvl`) / `/снятьвсеварны @цель` (`55+ lvl`)\n"
                "• `/кик @цель` (`45+ lvl`) — исключить из чата\n"
                "• `/бан @цель [причина]` (`55+ lvl`) / `/анбан @цель` (`55+ lvl`) / `/банлист` / `/чекбан`\n"
                "• `/выговор @цель [причина]` (`70+ lvl`) — выговор (`3/3` = снятие с должности)\n"
                "• `/выговоры @цель` / `/снятьвыговор @цель` / `/снятьвсевыговоры @цель` (`70+ lvl`)\n"
                "• `/очистить [1-100]` (`45+ lvl`) — массовая чистка чата\n"
                "• `/закреп` (реплай) / `/откреп` (`45+ lvl`) — закрепление сообщений\n"
                "• `/лок` / `/анлок` (`45+ lvl`) — закрыть или открыть беседу\n"
                "• `/антиспам` / `/антикапс` / `/антифлуд` / `/антиреклама` (`70+ lvl`)",
                keyboard=get_chat_inline_keyboard()
            )

        if section in ["роли", "ники", "roles", "nicks"]:
            return await message.answer(
                "🎭 ═══ НИКИ И РОЛИ ═══ 🎭\n\n"
                "• `/ник [текст]` (`0+ lvl`) — установить свой никнейм\n"
                "• `/нлист` (`0+ lvl`) — список всех ников в беседе\n"
                "• `/сник @цель [Ник]` (`60+ lvl`) — выдать ник участнику\n"
                "• `/делник @цель` (`60+ lvl`) — удалить ник участника\n"
                "• `/сетадмин @цель [уровень]` (`70+ lvl`) — выдать уровень прав\n"
                "• `/снятьроль @цель` (`70+ lvl`) — снять уровень (0 lvl)\n"
                "• `/повысить @цель [ур/шаг]` / `/понизить @цель [ур/шаг]` (`70+ lvl`)\n"
                "• `/новаяроль [ур] [Название]` (`90+ lvl`) — создать или переименовать роль\n"
                "• `/удалитьроль [ур]` (`90+ lvl`) — удалить роль из списка\n"
                "• `/сбросролей` (`90+ lvl`) — сбросить роли к стандартным",
                keyboard=get_chat_inline_keyboard()
            )

        if section in ["сеть", "г", "g", "чат", "chat"]:
            return await message.answer(
                "🌍 ═══ СЕТЕВЫЕ КОМАНДЫ И НАСТРОЙКИ ЧАТА ═══ 🌍\n\n"
                "• `/гбан @цель [причина]` (`90+ lvl`) / `/гунбан @цель` (`90+ lvl`)\n"
                "• `/гбанлист` (`60+ lvl`) / `/гчекбан @цель` (`45+ lvl`)\n"
                "• `/гмут @цель [мин]` / `/гранмут @цель` / `/гкик @цель` / `/гварн @цель [причина]` (`60+ lvl`)\n"
                "• `/гроль @цель [уровень]` / `/снятьгроль @цель` (`95+ lvl`)\n"
                "• `/гвыговор @цель [причина]` (`90+ lvl`) / `/гвыговоры` (`60+ lvl`) / `/гснятьвыговор` (`90+ lvl`)\n"
                "• `/гсозыв [текст]` (`70+ lvl`) / `/грассылка [текст]` *(владелец бота / 95+)*\n"
                "• `/гпроверка @цель` (`60+ lvl`) / `/гстата` (`60+ lvl`) / `/гчаты` (`60+ lvl`) / `/гтоп` (`45+ lvl`)\n"
                "• `/явладелец`, `/сетвладелец`, `/сбросвладельца`, `/сетназвание`, `/сетссылка`, `/сетправила`, `/сетпривет`, `/сетлидер`, `/репорт`",
                keyboard=get_chat_inline_keyboard()
            )

        return await message.answer(build_full_help_with_ranks(proj_name), keyboard=get_chat_inline_keyboard())

    # =================================================================
    # ИГРЫ (ПИНТИБОТ + КУБ + МОНЕТКА + РУЛЕТКА + ДУЭЛЬ + СЛОТЫ + КНБ + УГАДАЙ + ПРОЦЕНТ)
    # =================================================================
    elif cmd in ["пинтибот", "пинтбот", "pintibot", "pintbot", "пиво"]:
        res_txt = await run_pintibot(message, uid_int, peer_id, chat_data, params)
        return await message.answer(res_txt)

    elif cmd in ["куб", "кости", "dice"]:
        roll = secrets.randbelow(6) + 1
        dice_faces = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}
        return await message.answer(f"🎲 {await format_vk_user(uid_int)} бросил кубик и выбил {dice_faces[roll]} **`{roll}`**!")

    elif cmd in ["монетка", "coin", "flip"]:
        side = secrets.choice(["🪙 Орёл", "🪙 Решка"])
        return await message.answer(f"{side}! Выпало для {await format_vk_user(uid_int)}.")

    elif cmd in ["рулетка", "roulette"]:
        chamber = secrets.randbelow(6) + 1
        u_fmt = await format_vk_user(uid_int)
        if chamber == 1:
            return await message.answer(f"💥 **БАХ!** Патрон оказался в барабане (`{chamber}/6`) — {u_fmt} выбывает из раунда!")
        return await message.answer(f"🔫 *Щёлк...* Барабан прокручен (`{chamber}/6`) — {u_fmt} везунчик и остался в игре!")

    elif cmd in ["дуэль", "duel"]:
        tid, _ = await resolve_target(message, params)
        u_fmt = await format_vk_user(uid_int)
        if not tid or tid <= 0 or tid == uid_int:
            pool = [x for x in await get_chat_user_ids(peer_id, chat_data, uid_int) if x != uid_int]
            if not pool:
                return await message.answer("❌ Некого вызвать на дуэль — укажи цель: `/дуэль @ник`")
            tid = secrets.choice(pool)
        t_fmt = await format_vk_user(tid)
        s1 = secrets.randbelow(100) + 1
        s2 = secrets.randbelow(100) + 1
        if s1 == s2:
            return await message.answer(f"⚔️ **ДУЭЛЬ:** {u_fmt} (`{s1}`) vs {t_fmt} (`{s2}`) — **НИЧЬЯ!**")
        winner, loser = (u_fmt, t_fmt) if s1 > s2 else (t_fmt, u_fmt)
        w_pts, l_pts = max(s1, s2), min(s1, s2)
        return await message.answer(
            f"⚔️ **ДУЭЛЬ ЗАВЕРШЕНА!**\n"
            f"• {u_fmt}: `{s1}` | {t_fmt}: `{s2}`\n"
            f"• Победитель: {winner} (`{w_pts}` против `{l_pts}`)!"
        )

    elif cmd in ["слоты", "slots", "казино"]:
        reels = ["🍒", "🍋", "🔔", "💎", "7️⃣"]
        r1, r2, r3 = secrets.choice(reels), secrets.choice(reels), secrets.choice(reels)
        u_fmt = await format_vk_user(uid_int)
        if r1 == r2 == r3:
            res = "🏆 **ДЖЕКПОТ!** Три одинаковых символа!"
        elif r1 == r2 or r2 == r3 or r1 == r3:
            res = "✨ **ВЫИГРЫШ!** Совпали два символа!"
        else:
            res = "💨 Мимо — попробуй ещё раз!"
        return await message.answer(f"🎰 `[ {r1} | {r2} | {r3} ]` — {u_fmt}\n{res}")

    elif cmd in ["камень", "кнб", "rps"]:
        choice_map = {
            "к": "камень", "камень": "камень", "rock": "камень",
            "н": "ножницы", "ножницы": "ножницы", "scissors": "ножницы",
            "б": "бумага", "бумага": "бумага", "paper": "бумага",
        }
        raw_c = params[0].lower() if params else ""
        player_c = choice_map.get(raw_c)
        if not player_c:
            return await message.answer("❌ Формат: `/камень [камень/ножницы/бумага]` (или `к / н / б`)")
        bot_c = secrets.choice(["камень", "ножницы", "бумага"])
        wins_against = {"камень": "ножницы", "ножницы": "бумага", "бумага": "камень"}
        u_fmt = await format_vk_user(uid_int)
        if player_c == bot_c:
            outcome = "🤝 Ничья!"
        elif wins_against[player_c] == bot_c:
            outcome = f"🎉 {u_fmt} побеждает!"
        else:
            outcome = "🤖 Бот побеждает!"
        return await message.answer(f"✊ Ты выбрал: **{player_c}** | Бот выбрал: **{bot_c}**\n{outcome}")

    elif cmd in ["угадай", "guess"]:
        if not params or not params[0].isdigit():
            return await message.answer("❌ Формат: `/угадай [число от 1 до 10]`")
        guess_num = int(params[0])
        if guess_num < 1 or guess_num > 10:
            return await message.answer("❌ Выбери число от `1` до `10`.")
        secret_num = secrets.randbelow(10) + 1
        u_fmt = await format_vk_user(uid_int)
        if guess_num == secret_num:
            return await message.answer(f"🎯 Точно в цель! {u_fmt} угадал число **`{secret_num}`**!")
        return await message.answer(f"❌ Не угадал! Ты назвал `{guess_num}`, а я загадал **`{secret_num}`**.")

    elif cmd in ["процент", "percent"]:
        pct = secrets.randbelow(101)
        q = " ".join(params) if params else "этого"
        return await message.answer(f"📈 Измеритель показывает **`{pct}%`** для: *{q}*")

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
        pool_ids = await get_chat_user_ids(peer_id, chat_data, uid_int)
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
        is_vk_owner, is_vk_admin = await get_vk_chat_member_flags(peer_id, uid_int)
        if not (is_vk_owner or is_vk_admin or (MAIN_OWNER_ID and uid_int == MAIN_OWNER_ID)):
            return await message.answer(
                "⛔ Отказано! Команда `/явладелец` доступна только создателю или администратору беседы ВК (со звездой)."
            )
        sync_owner_member(chat_data, int(uid_int))
        save_db(db)
        add_log(f"Владелец чата {peer_id} закреплён через /явладелец: {await format_vk_user(uid_int)}")
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
            f"• Назначенных админов в беседе: `{sum(1 for _, v in chat_data.get('members', {}).items() if (v.get('level', 0) if isinstance(v, dict) else int(v or 0)) > 0)}`"
        )

    elif cmd in ["info", "инфо"]:
        owner_fmt = await format_vk_user(int(owner_id)) if owner_id else "Не определён"
        antispam_str = "🟢 Вкл" if chat_data.get("antispam") else "🔴 Выкл"
        caps_str = "🟢 Вкл" if chat_data.get("caps_guard") else "🔴 Выкл"
        flood_str = "🟢 Вкл" if chat_data.get("antiflood") else "🔴 Выкл"
        ads_str = "🟢 Вкл" if chat_data.get("antiads") else "🔴 Выкл"
        lock_str = "🔒 Закрыт" if chat_data.get("locked") else "🔓 Открыт"
        return await message.answer(
            f"ℹ️ ═══ ИНФОРМАЦИЯ О БЕСЕДЕ ═══ ℹ️\n\n"
            f"• Беседа: **{chat_title}**\n"
            f"• Проект: **{proj_name}**\n"
            f"• Peer ID: `{peer_id}`\n"
            f"• Владелец: {owner_fmt}\n"
            f"• Режим чата: {lock_str}\n"
            f"• Защита: Спам {antispam_str} | Капс {caps_str} | Флуд {flood_str} | Реклама {ads_str}\n"
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
    # НИКИ И РОЛИ (ДОСТУПНЫ СРАЗУ, БЕЗ ПУЛОВ)
    # =================================================================
    elif cmd in ["ник", "nick"]:
        if not params:
            return await message.answer("❌ Формат: `/ник [Ваш никнейм]`")
        new_nick = " ".join(params).strip()
        if len(new_nick) > 40:
            return await message.answer("❌ Ник не длиннее 40 символов.")
        current_lvl = get_member_level_raw(chat_data, uid_int)
        set_member_level(chat_data, uid_int, current_lvl, nickname=new_nick)
        save_db(db)
        return await message.answer(f"✅ Ваш ник в беседе обновлён на: **{new_nick}**")

    elif cmd in ["nlist", "нлист"]:
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
        current_lvl = get_member_level_raw(chat_data, target_id)
        set_member_level(chat_data, target_id, current_lvl, nickname=new_nick)
        save_db(db)
        return await message.answer(f"✅ Ник **{new_nick}** выдан {await format_vk_user(target_id)}.")

    elif cmd in ["delnick", "делник"]:
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
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        chat_data["role_names"] = {str(k): v for k, v in DEFAULT_ROLES.items()}
        save_db(db)
        return await message.answer("♻️ Список ролей беседы сброшен к стандартному.")

    elif cmd in ["setadmin", "сетадмин", "выдатьадм", "выдатьроль", "роль"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/сетадмин [@ник/id/ответ] [уровень]`")
        lvl_val = next((int(p) for p in rem if p.isdigit()), None)
        if lvl_val is None:
            return await message.answer("❌ Укажите числовой уровень админки, например: `/сетадмин @id1 70`")
        lvl_val = max(0, min(100, lvl_val))
        set_member_level(chat_data, int(tid), lvl_val)
        if lvl_val >= 100 and chat_data.get("owner_id") is None:
            chat_data["owner_id"] = int(tid)
        save_db(db)
        t_fmt = await format_vk_user(tid)
        add_log(f"Выдача роли {lvl_val} lvl для {t_fmt} от {await format_vk_user(uid_int)}")
        return await message.answer(f"✅ {t_fmt} присвоен ранг `{lvl_val} lvl` (**{get_role_title(lvl_val, chat_data)}**).")

    elif cmd in ["повысить", "promote"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/повысить [@ник/id/ответ] [новый_уровень]`")
        cur_lvl = get_member_level_raw(chat_data, tid)
        new_lvl = int(rem[0]) if rem and rem[0].isdigit() else min(95, cur_lvl + 15)
        new_lvl = max(0, min(100, new_lvl))
        set_member_level(chat_data, int(tid), new_lvl)
        save_db(db)
        t_fmt = await format_vk_user(tid)
        add_log(f"Повышение {t_fmt} до {new_lvl} lvl от {await format_vk_user(uid_int)}")
        return await message.answer(f"⬆️ {t_fmt} повышен до `{new_lvl} lvl` (**{get_role_title(new_lvl, chat_data)}**).")

    elif cmd in ["понизить", "demote"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/понизить [@ник/id/ответ] [новый_уровень]`")
        cur_lvl = get_member_level_raw(chat_data, tid)
        new_lvl = int(rem[0]) if rem and rem[0].isdigit() else max(0, cur_lvl - 15)
        new_lvl = max(0, min(100, new_lvl))
        set_member_level(chat_data, int(tid), new_lvl)
        save_db(db)
        t_fmt = await format_vk_user(tid)
        add_log(f"Понижение {t_fmt} до {new_lvl} lvl от {await format_vk_user(uid_int)}")
        return await message.answer(f"⬇️ {t_fmt} понижен до `{new_lvl} lvl` (**{get_role_title(new_lvl, chat_data)}**).")

    elif cmd in ["removerole", "снятьроль", "снятадм"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        set_member_level(chat_data, int(tid), 0)
        db.get("global_roles", {}).pop(str(int(tid)), None)
        if chat_data.get("owner_id") == int(tid) and uid_int != int(tid):
            chat_data["owner_id"] = None
        save_db(db)
        t_fmt = await format_vk_user(tid)
        add_log(f"Снятие админки с {t_fmt} от {await format_vk_user(uid_int)}")
        return await message.answer(f"🟢 С {t_fmt} сняты права админки (0 lvl).")

    # =================================================================
    # ВЫГОВОРЫ АДМИНАМ И МОДЕРАЦИЯ
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
            set_member_level(chat_data, int(tid), 0)
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
        target_lvl = get_user_level_by_id(target_id, chat_data, owner_id)
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
        target_lvl = get_user_level_by_id(target_id, chat_data, owner_id)
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
        if chat_id_num:
            try:
                await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=tid)
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
        if not chat_id_num:
            return await message.answer("❌ Команда работает только в беседе.")
        try:
            await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=tid)
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
            if chat_id_num:
                try:
                    await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=tid)
                except Exception:
                    pass
            chat_data["warns"][t_str] = []
            save_db(db)
            await message.answer(f"🚫 Лимит исчерпан! {u_fmt} — авто-кик за 3 предупреждения.")

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

    elif cmd in ["antiads", "антиреклама"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        chat_data["antiads"] = not bool(chat_data.get("antiads"))
        save_db(db)
        state_txt = "🟢 ВКЛЮЧЁН" if chat_data["antiads"] else "🔴 ВЫКЛЮЧЕН"
        return await message.answer(f"🚫 Режим антирекламы сторонних чатов/групп: {state_txt}.")

    elif cmd in ["clear", "очистить", "чистка"]:
        if user_lvl < 45:
            return await message.answer(f"❌ Требуется 45+ уровень (у вас: {user_lvl}).")
        count = int(params[0]) if params and params[0].isdigit() else 10
        count = max(1, min(count, 100))
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
    # СЕТЕВЫЕ КОМАНДЫ НА /г... И /g...
    # =================================================================
    elif cmd in ["grole", "гроль"]:
        if user_lvl < 95:
            return await message.answer(f"❌ Требуется 95+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid or not rem or not rem[0].isdigit():
            return await message.answer("❌ Формат: `/гроль [@ник/id] [уровень]`")
        g_lvl = max(0, min(100, int(rem[0])))
        db["global_roles"][str(tid)] = g_lvl
        save_db(db)
        t_fmt = await format_vk_user(tid)
        add_log(f"Глобальная роль {g_lvl} lvl для {t_fmt} от {await format_vk_user(uid_int)}")
        return await message.answer(f"🌐 Глобальная роль `{g_lvl}` выдана {t_fmt} по всей сети.")

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
        if chat_id_num:
            try:
                await bot.api.messages.remove_chat_user(chat_id=chat_id_num, user_id=tid)
            except Exception:
                pass
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

    elif cmd in ["гмут", "gmute"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/гмут [@ник/id/ответ] [минуты]`")
        mins = int(rem[0]) if rem and rem[0].isdigit() else 15
        applied = 0
        for cid in db.get("global_chats", []):
            c_info = get_chat_data(int(cid))
            c_info["mutes"][str(tid)] = time.time() + (mins * 60)
            applied += 1
        save_db(db)
        return await message.answer(f"🌍🤫 {await format_vk_user(tid)} замучен на `{mins}` мин в беседах сети (`{applied}`).")

    elif cmd in ["гранмут", "gunmute"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        for cid in db.get("global_chats", []):
            c_info = get_chat_data(int(cid))
            c_info.get("mutes", {}).pop(str(tid), None)
        save_db(db)
        return await message.answer(f"🌍🔊 Сетевой мут снят с {await format_vk_user(tid)}.")

    elif cmd in ["гкик", "gkick"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Укажите пользователя.")
        kicked = 0
        for cid in db.get("global_chats", []):
            if int(cid) > 2000000000:
                try:
                    await bot.api.messages.remove_chat_user(chat_id=int(cid) - 2000000000, user_id=tid)
                    kicked += 1
                except Exception:
                    pass
        return await message.answer(f"🌍🥾 {await format_vk_user(tid)} исключён из бесед сети (`{kicked}`).")

    elif cmd in ["гварн", "gwarn"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/гварн [@ник/id/ответ] [причина]`")
        rsn = " ".join(rem) if rem else "Сетевое предупреждение"
        t_str = str(tid)
        for cid in db.get("global_chats", []):
            c_info = get_chat_data(int(cid))
            c_info.setdefault("warns", {}).setdefault(t_str, []).append(f"[СЕТЬ] {rsn}")
        save_db(db)
        return await message.answer(f"🌍⚠️ {await format_vk_user(tid)} получил сетевой варн. Причина: {rsn}")

    elif cmd in ["гвыговор", "greprimand"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, rem = await resolve_target(message, params)
        if not tid:
            return await message.answer("❌ Формат: `/гвыговор [@ник/id/ответ] [причина]`")
        rsn = " ".join(rem) if rem else "Нарушение сетевого регламента"
        t_str = str(tid)
        db["global_reprimands"].setdefault(t_str, []).append(rsn)
        cnt = len(db["global_reprimands"][t_str])
        save_db(db)
        return await message.answer(f"🌍📕 {await format_vk_user(tid)} получил сетевой выговор (`{cnt}`). Причина: {rsn}")

    elif cmd in ["гвыговоры", "greprimands"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            tid = uid_int
        r_list = db.get("global_reprimands", {}).get(str(tid), [])
        u_fmt = await format_vk_user(tid)
        if not r_list:
            return await message.answer(f"🌍 У {u_fmt} нет сетевых выговоров.")
        lines = [f"{i + 1}. {r}" for i, r in enumerate(r_list)]
        return await message.answer(f"🌍📕 **Сетевые выговоры {u_fmt} (`{len(r_list)}`):**\n\n" + "\n".join(lines))

    elif cmd in ["гснятьвыговор", "gunreprimand"]:
        if user_lvl < 90:
            return await message.answer(f"❌ Требуется 90+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if tid and str(tid) in db.get("global_reprimands", {}) and db["global_reprimands"][str(tid)]:
            db["global_reprimands"][str(tid)].pop()
            save_db(db)
            return await message.answer(f"🌍🟢 Последний сетевой выговор снят у {await format_vk_user(tid)}.")
        return await message.answer("❌ У игрока нет сетевых выговоров.")

    elif cmd in ["гсозыв", "gcall"]:
        if user_lvl < 70:
            return await message.answer(f"❌ Требуется 70+ уровень (у вас: {user_lvl}).")
        msg_call = " ".join(params).strip() if params else "Сетевой созыв администрации!"
        sent = 0
        for cid in db.get("global_chats", []):
            try:
                await bot.api.messages.send(
                    peer_id=int(cid),
                    message=f"🌍📣 **СЕТЕВОЙ СОЗЫВ**\n• От: {await format_vk_user(uid_int)}\n• Сообщение: **{msg_call}**\n\n@all",
                    random_id=0
                )
                sent += 1
            except Exception:
                pass
        return await message.answer(f"🌍📣 Сетевой созыв отправлен в `{sent}` бесед.")

    elif cmd in ["грассылка", "gbroadcast"]:
        if uid_int != MAIN_OWNER_ID and user_lvl < 95:
            return await message.answer("❌ Доступно только владельцу бота или 95+ lvl.")
        body = " ".join(params).strip()
        if not body:
            return await message.answer("❌ Формат: `/грассылка [текст объявления]`")
        sent = await run_global_broadcast(uid_int, body)
        return await message.answer(f"🚀 Сетевая рассылка отправлена в `{sent}` бесед.")

    elif cmd in ["gcheck", "гпроверка"]:
        if user_lvl < 60:
            return await message.answer(f"❌ Требуется 60+ уровень (у вас: {user_lvl}).")
        tid, _ = await resolve_target(message, params)
        if not tid:
            tid = uid_int
        t_str = str(tid)
        g_lvl = db.get("global_roles", {}).get(t_str, 0)
        g_ban = db.get("global_bans", {}).get(t_str)
        g_vigs = len(db.get("global_reprimands", {}).get(t_str, []))
        ldr = db.get("leaders", {}).get(t_str, "Нет")
        return await message.answer(
            f"🌍 ═══ СЕТЕВОЕ ДОСЬЕ ═══ 🌍\n"
            f"👤 {await format_vk_user(tid)}\n\n"
            f"• Глобальный уровень: `{g_lvl} lvl`\n"
            f"• Сетевых выговоров: `{g_vigs}`\n"
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
            f"🌍 ═══ СЕТЕВАЯ СТАТИСТИКА ═══ 🌍\n\n"
            f"• Всего бесед бота: `{len(db.get('global_chats', []))}`\n"
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
                r_name = get_role_title(lvl, chat_data, owner_flag=(lvl >= 100))
                nick_str = f" — `{info.get('nickname')}`" if isinstance(info, dict) and info.get("nickname") else ""
                lines.append(f"• {await format_vk_user(uid_item)}{nick_str} ➔ **{r_name}** `[{lvl} lvl]`")

        for g_id, lvl in db.get("global_roles", {}).items():
            uid_item = int(g_id)
            if uid_item in shown_ids or int(lvl) <= 0:
                continue
            shown_ids.add(uid_item)
            r_name = get_role_title(int(lvl), chat_data, owner_flag=(int(lvl) >= 100))
            lines.append(f"• 🌐 {await format_vk_user(uid_item)} ➔ **{r_name}** `[{lvl} lvl]`")

        if not lines:
            return await message.answer("👑 В этом чате пока не определён владелец и нет назначенных администраторов.")

        return await message.answer("👑 ═══ АДМИНИСТРАЦИЯ БЕСЕДЫ ═══ 👑\n\n" + "\n".join(lines), keyboard=get_chat_inline_keyboard())

    elif cmd in ["staff", "состав"]:
        parts = []
        shown_staff = set()
        if owner_id:
            shown_staff.add(int(owner_id))
            parts.append(f"👑 **Владелец чата:** {await format_vk_user(int(owner_id))} (`100 lvl`)")

        adm_lines = []
        for m_id, info in chat_data.get("members", {}).items():
            uid_item = int(m_id)
            lvl = info.get("level", 0) if isinstance(info, dict) else int(info or 0)
            if lvl > 0 and uid_item not in shown_staff:
                shown_staff.add(uid_item)
                adm_lines.append(f"• {await format_vk_user(uid_item)} — **{get_role_title(lvl, chat_data, owner_flag=(lvl >= 100))}** `[{lvl} lvl]`")
        for g_id, lvl in db.get("global_roles", {}).items():
            uid_item = int(g_id)
            if int(lvl) > 0 and uid_item not in shown_staff:
                shown_staff.add(uid_item)
                adm_lines.append(f"• 🌐 {await format_vk_user(uid_item)} — **{get_role_title(int(lvl), chat_data, owner_flag=(int(lvl) >= 100))}** `[{lvl} lvl]`")
        if adm_lines:
            parts.append("🛡 **Администраторы:**\n" + "\n".join(adm_lines))

        if db.get("leaders"):
            lead_lines = [f"• {await format_vk_user(int(l_id))} — **{org}**" for l_id, org in db["leaders"].items()]
            parts.append("👔 **Лидеры:**\n" + "\n".join(lead_lines))

        if not parts:
            return await message.answer("📋 Состав беседы пока пуст.")
        return await message.answer("📋 ═══ СОСТАВ БЕСЕДЫ ═══ 📋\n\n" + "\n\n".join(parts))


# =====================================================================
# 8. ЗАПУСК БОТА + ЛЁГКИЙ ПОРТ ДЛЯ RENDER WEB SERVICE
# =====================================================================
async def start_http_keepalive() -> web.AppRunner:
    port = int(os.environ.get("PORT", "10000"))
    app = web.Application()

    async def health_check(request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"[HTTP KEEPALIVE] Listening on 0.0.0.0:{port}")
    return runner


async def main() -> None:
    runner = await start_http_keepalive()
    try:
        while True:
            try:
                await bot.run_polling()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[POLLING ERROR] {e}")
                await asyncio.sleep(5)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
