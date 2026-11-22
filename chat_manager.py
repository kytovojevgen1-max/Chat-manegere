import asyncio
import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web
from vkbottle import CtxStorage, Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Bot, Message

# ==============================================================================
# КОНФИГУРАЦИЯ
# ==============================================================================
TOKEN = os.getenv(
    "VK_TOKEN",
    "vk1.a.ONWFvnPZaVTXfIN1NsuwhyGfU6LsNurMI8qT_pVrqm_pxrdIBRvh_ibzoBVUmX5DhOTv26MSpMPmeKEwCZvtIbjfCByslMQ7_-K7o8ipviBYTbslgWeMHoDeWZ5HXtBVPNNCGfkXdK0DsjOe71hXQBXPvfI6K1h3S4YfrPAd6k6I1u8v4Nmp5tZSmZkgISWrhX7WtIcbWkteWFeFXrUd-g",
)
OWNER_IDS = {
    int(x)
    for x in os.getenv("OWNER_IDS", "897082738").split(",")
    if x.strip().isdigit()
}
DB_FILE = os.getenv("DB_FILE", "games_chat_manager_db.json")
PORT = int(os.getenv("PORT", "10000"))

bot = Bot(token=TOKEN)
ctx = CtxStorage()

# ==============================================================================
# БАЗА ДАННЫХ (АТОМАРНОЕ СОХРАНЕНИЕ)
# ==============================================================================
DEFAULT_DB: Dict[str, Any] = {
    "chats": {},
    "global_admins": {},
    "reports": [],
    "economy": {},
}

# Стандартные ранги команд (меняются для конкретной беседы через /кмд [команда] [ранг])
DEFAULT_CMD_RANKS: Dict[str, int] = {
    "ник": 0,
    "сник": 15,
    "мут": 30,
    "размут": 30,
    "варн": 40,
    "разварн": 40,
    "закреп": 40,
    "заметка": 40,
    "удалзаметку": 40,
    "кик": 50,
    "чистка": 50,
    "созыв": 50,
    "репорты": 50,
    "ответ": 50,
    "бан": 60,
    "разбан": 60,
    "чатлок": 60,
    "защита": 60,
    "антиспам": 60,
    "антикапс": 60,
    "антифлуд": 60,
    "антиреклама": 60,
    "антимат": 60,
    "антибот": 60,
    "лидер": 60,
    "приветствие": 60,
    "аварн": 75,
    "снятьаварн": 75,
    "ранг": 80,
    "доступ": 95,
    "кмд": 100,   # строго с 100 lvl
}

BUSINESS_CATALOG: Dict[str, Dict[str, Any]] = {
    "1": {"name": "Ларёк с шаурмой", "cost": 5000, "income": 600},
    "2": {"name": "Автомойка", "cost": 25000, "income": 3200},
    "3": {"name": "Клуб / Бар", "cost": 100000, "income": 14000},
    "4": {"name": "IT-Студия", "cost": 350000, "income": 52000},
    "5": {"name": "Нефтевышка", "cost": 1200000, "income": 190000},
}

# Базовые маркеры жёсткого мата / оскорблений для /антимат
PROFANITY_PATTERNS = [
    r"\b(?:сук[аиу]|бля[дт]|бл[яи]|ху[йеёюя]|пизд[аеиуё]|еба[нлть]|ёб[ануты]|муд[аои]|гандон|уёб\w*)\b",
]


def load_db() -> Dict[str, Any]:
    if not os.path.exists(DB_FILE):
        return json.loads(json.dumps(DEFAULT_DB))
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT_DB.items():
            if k not in data:
                data[k] = json.loads(json.dumps(v))
        return data
    except Exception:
        return json.loads(json.dumps(DEFAULT_DB))


def save_db(data: Dict[str, Any]) -> None:
    tmp_file = f"{DB_FILE}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, DB_FILE)


db = load_db()


def get_chat(peer_id: int) -> Dict[str, Any]:
    pid = str(peer_id)
    if pid not in db["chats"]:
        db["chats"][pid] = {
            "title": f"Чат {peer_id}",
            "locked": False,
            "antispam": False,
            "anticaps": False,
            "antiflood": False,
            "antilink": False,
            "antiswear": False,
            "antibot": False,
            "welcome": "",
            "ranks": {},
            "cmd_ranks": {},
            "warns": {},
            "admin_warns": {},
            "mutes": {},
            "bans": {},
            "rep": {},
            "notes": {},
            "leaders": {},
            "nicks": {},
            "marriages": {},
        }
        save_db(db)
    chat = db["chats"][pid]
    for k, v in [
        ("cmd_ranks", {}),
        ("admin_warns", {}),
        ("marriages", {}),
        ("welcome", ""),
        ("antiswear", False),
        ("antibot", False),
    ]:
        if k not in chat:
            chat[k] = v
    return chat


def get_econ(user_id: int) -> Dict[str, Any]:
    uid = str(user_id)
    if uid not in db["economy"]:
        db["economy"][uid] = {
            "balance": 2500,
            "bank": 0,
            "last_bonus": 0,
            "last_work": 0,
            "businesses": {},
            "last_collect": int(time.time()),
        }
        save_db(db)
    return db["economy"][uid]


def is_owner(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in OWNER_IDS)


def get_rank(peer_id: int, user_id: int) -> int:
    if is_owner(user_id):
        return 100
    g_rank = int(db["global_admins"].get(str(user_id), 0))
    c_rank = 0
    if peer_id > 2000000000:
        chat = get_chat(peer_id)
        c_rank = int(chat["ranks"].get(str(user_id), 0))
    return max(g_rank, c_rank)


def get_req_rank(peer_id: int, cmd_key: str) -> int:
    if cmd_key == "кмд":
        return 100
    default_lvl = DEFAULT_CMD_RANKS.get(cmd_key, 0)
    if peer_id > 2000000000:
        chat = get_chat(peer_id)
        if cmd_key in chat.get("cmd_ranks", {}):
            return int(chat["cmd_ranks"][cmd_key])
    return default_lvl


def has_cmd_access(peer_id: int, user_id: int, cmd_key: str) -> Tuple[bool, int]:
    req = get_req_rank(peer_id, cmd_key)
    cur = get_rank(peer_id, user_id)
    return (cur >= req), req


async def get_user_name(user_id: int) -> str:
    try:
        users = await bot.api.users.get(user_ids=[user_id])
        if users:
            return f"[id{user_id}|{users[0].first_name} {users[0].last_name}]"
    except Exception:
        pass
    return f"[id{user_id}|Пользователь {user_id}]"


def parse_target_id(msg: Message, arg: Optional[str] = None) -> Optional[int]:
    if msg.reply_message and msg.reply_message.from_id:
        return msg.reply_message.from_id
    if msg.fwd_messages and msg.fwd_messages[0].from_id:
        return msg.fwd_messages[0].from_id
    if arg:
        m = re.search(r"\[id(\d+)\|", arg)
        if m:
            return int(m.group(1))
        m2 = re.search(r"vk\.com/(?:id)?(\d+)", arg)
        if m2:
            return int(m2.group(1))
        m3 = re.search(r"@id(\d+)", arg)
        if m3:
            return int(m3.group(1))
        digits = re.sub(r"\D", "", arg)
        if digits.isdigit():
            return int(digits)
    return None


def fmt_num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def normalize_chat_text(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(r"^\[(?:club|public)\d+\|[^\]]+\]\s*,?\s*", "", s, flags=re.I)
    s = re.sub(r"^@[A-Za-z0-9_.]+\s*,?\s*", "", s)
    return s.strip()


def split_tokens(s: str) -> List[str]:
    return [t for t in re.split(r"\s+", s.strip()) if t]


def parse_toggle_arg(arg: Optional[str], current_state: bool) -> bool:
    if not arg:
        return not current_state
    low = arg.strip().lower()
    if low in ("вкл", "on", "1", "да", "true", "+"):
        return True
    if low in ("выкл", "off", "0", "нет", "false", "-"):
        return False
    return not current_state


# ==============================================================================
# КНОПКИ (МЕНЮ / ПАНЕЛЬ / БЫСТРЫЕ КНОПКИ)
# ==============================================================================
def build_start_keyboard(user_id: int) -> Keyboard:
    kb = (
        Keyboard(inline=True)
        .add(Text("📋 Команды", {"cmd": "help"}), color=KeyboardButtonColor.PRIMARY)
        .add(Text("⚙️ Панель", {"cmd": "panel"}), color=KeyboardButtonColor.SECONDARY)
        .row()
        .add(Text("🎰 Игры и Бизнес", {"cmd": "games"}), color=KeyboardButtonColor.POSITIVE)
        .add(Text("📊 Моя стата", {"cmd": "stats"}), color=KeyboardButtonColor.SECONDARY)
    )
    if is_owner(user_id):
        kb.row().add(Text("💾 Скачать БД", {"cmd": "dump_db"}), color=KeyboardButtonColor.NEGATIVE)
    return kb


def build_panel_keyboard(user_id: int, is_chat: bool) -> Keyboard:
    kb = (
        Keyboard(inline=True)
        .add(Text("🛡 Модерация", {"cmd": "mod_help"}), color=KeyboardButtonColor.PRIMARY)
        .add(Text("🚨 Защита чата", {"cmd": "guard_status"}), color=KeyboardButtonColor.PRIMARY)
        .row()
        .add(Text("🎰 Игры и Бизнес", {"cmd": "games"}), color=KeyboardButtonColor.POSITIVE)
        .add(Text("📊 Моя стата", {"cmd": "stats"}), color=KeyboardButtonColor.SECONDARY)
        .row()
        .add(Text("📩 Репорты", {"cmd": "reports"}), color=KeyboardButtonColor.SECONDARY)
    )
    if is_chat:
        kb.add(Text("🔐 Ранги (/кмд)", {"cmd": "cmd_ranks_list"}), color=KeyboardButtonColor.SECONDARY)
    if is_owner(user_id):
        kb.row().add(Text("💾 Скачать БД", {"cmd": "dump_db"}), color=KeyboardButtonColor.NEGATIVE)
    return kb


# ==============================================================================
# АВТОМОДЕРАЦИЯ, ЗАЩИТА И ПРИВЕТСТВИЯ В БЕСЕДАХ
# ==============================================================================
_last_msg_time: Dict[Tuple[int, int], float] = {}
_last_msg_text: Dict[Tuple[int, int], str] = {}


async def run_chat_guard(msg: Message) -> bool:
    if msg.peer_id <= 2000000000 or not msg.from_id:
        return False

    peer_id = msg.peer_id
    user_id = msg.from_id
    chat = get_chat(peer_id)

    if msg.action and msg.action.type.value in ("chat_invite_user", "chat_invite_user_by_link"):
        invited_id = msg.action.member_id or user_id
        if invited_id < 0 and chat.get("antibot"):
            try:
                await bot.api.messages.remove_chat_user(chat_id=peer_id - 2000000000, member_id=invited_id)
                await msg.answer("🤖 Включён **Антибот**: сторонний бот автоматически исключён из беседы.")
            except Exception:
                pass
            return True
        if str(invited_id) in chat.get("bans", {}):
            try:
                await bot.api.messages.remove_chat_user(chat_id=peer_id - 2000000000, member_id=invited_id)
                await msg.answer(f"🔨 {await get_user_name(invited_id)} находится в бане этой беседы и был исключён.")
            except Exception:
                pass
            return True
        if chat.get("welcome"):
            w_text = chat["welcome"].replace("{user}", await get_user_name(invited_id))
            await msg.answer(w_text)
        return True

    if user_id < 0:
        return False

    if chat.get("title") == f"Чат {peer_id}":
        try:
            conv = await bot.api.messages.get_conversations_by_id(peer_ids=[peer_id])
            if conv.items and conv.items[0].chat_settings:
                chat["title"] = conv.items[0].chat_settings.title
                save_db(db)
        except Exception:
            pass

    rank = get_rank(peer_id, user_id)
    now = time.time()

    mute_until = float(chat["mutes"].get(str(user_id), 0))
    if mute_until > now and rank < 30:
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
        except Exception:
            pass
        return True
    elif mute_until and mute_until <= now:
        chat["mutes"].pop(str(user_id), None)
        save_db(db)

    if chat.get("locked") and rank < 50:
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
        except Exception:
            pass
        return True

    text = (msg.text or "").strip()
    if not text or rank >= 30:
        return False

    # 1. Антиреклама / антиссылки
    if chat.get("antilink") and re.search(
        r"(https?://|t\.me/|vk\.me/|vk\.cc/|vk\.com/join|discord\.gg/|@[\w_]+)", text, re.I
    ):
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
            await msg.answer(f"🚫 {await get_user_name(user_id)}, реклама и ссылки в этой беседе запрещены!")
        except Exception:
            pass
        return True

    # 2. Антимат
    if chat.get("antiswear") and any(re.search(pat, text, re.I) for pat in PROFANITY_PATTERNS):
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
            await msg.answer(f"🤬 {await get_user_name(user_id)}, следите за выражениями (включён антимат).")
        except Exception:
            pass
        return True

    # 3. Антикапс
    letters = [c for c in text if c.isalpha()]
    if chat.get("anticaps") and len(letters) >= 8:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio >= 0.75:
            try:
                await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
                await msg.answer(f"🔠 {await get_user_name(user_id)}, не капси в чате!")
            except Exception:
                pass
            return True

    # 4. Антифлуд и антиспам
    key = (peer_id, user_id)
    if chat.get("antiflood") and now - _last_msg_time.get(key, 0) < 1.2:
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
        except Exception:
            pass
        return True
    if chat.get("antispam") and len(text) >= 5 and _last_msg_text.get(key) == text.lower():
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
        except Exception:
            pass
        return True

    _last_msg_time[key] = now
    _last_msg_text[key] = text.lower()
    return False


# ==============================================================================
# СПРАВОЧНИКИ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==============================================================================
HELP_TEXT = (
    "🤖 **GAMES | Чат Менеджер**\n\n"
    "🚨 **Защита беседы (работают с `/` и без `/`):**\n"
    "• `/антиреклама [вкл|выкл]` (или `антиреклама`) — блок ссылок, инвайтов и рекламы\n"
    "• `/антикапс [вкл|выкл]` (или `антикапс`) — блок КАПСА\n"
    "• `/антиспам [вкл|выкл]` (или `антиспам`) — блок повторов сообщений\n"
    "• `/антифлуд [вкл|выкл]` (или `антифлуд`) — блок частой отправки сообщений\n"
    "• `/антимат [вкл|выкл]` (или `антимат`) — блок грубого мата\n"
    "• `/антибот [вкл|выкл]` (или `антибот`) — автокик добавляемых ботов\n"
    "• `/защита [модуль] [вкл|выкл]` и `защиты` — статус всех модулей\n\n"
    "🛡 **Модерация (в беседах):**\n"
    "• `мут [юзер] [мин] [причина]` / `/мут ...` | `размут [юзер]` / `/размут ...`\n"
    "• `варн [юзер] [причина]` / `/варн ...` | `разварн [юзер]` / `/разварн ...`\n"
    "• `кик [юзер] [причина]` / `/кик ...` | `бан [юзер] [причина]` / `/бан ...` | `разбан [id]` / `/разбан ...`\n"
    "• `чистка [1-100]` / `/чистка ...` | `закреп` / `/закреп` (ответом)\n"
    "• `чат лок` / `чат анлок` | `приветствие [текст|выкл]`\n\n"
    "👑 **Админка, ранги команд (/кмд) и репорты:**\n"
    "• `ранг [юзер] [0-100]` / `/ранг ...` | `стата [юзер]` / `/стата ...` | `админы` / `/админы`\n"
    "• `/кмд [команда] [ранг 0-100]` — поменять нужный ранг ЛЮБОЙ команды ТОЛЬКО в этой беседе (строго 100 lvl)\n"
    "• `/доступ [команда] [ранг]` — настройка прав для беседы (с 95+ lvl)\n"
    "• `аварн [юзер] [причина]` / `/аварн ...` | `снятьаварн [юзер]` / `/снятьаварн ...`\n"
    "• `репорт [текст]` / `/репорт ...` | `репорты` / `/репорты` | `ответ [ID] [текст]` / `/ответ ...`\n\n"
    "📋 **Ники, лидеры, фан и утилиты:**\n"
    "• `ник [новый_ник]` / `/ник ...` | `сник [юзер] [новый_ник]` / `/сник ...`\n"
    "• `лидер [фракция] [юзер]` / `/лидер ...` | `лидеры` / `/лидеры`\n"
    "• `заметка [имя] [текст]` | `заметки` | `удалзаметку [имя]`\n"
    "• `+реп` / `-реп` (ответом) | `топ` | `созыв [текст]`\n"
    "• `брак [юзер]` | `развод` | `шар [вопрос]` | `выбор [А или Б]`\n\n"
    "🎰 **Игры, казино и бизнесы:**\n"
    "• `баланс` / `/баланс` / `/б` | `бонус` | `работа` | `перевод [юзер] [сумма]`\n"
    "• `казино [ставка]` | `колесо [ставка]` | `слоты [ставка]` | `кубик [ставка]`\n"
    "• `бизнесы` | `купитьбиз [номер]` | `мойбиз` | `собрать`\n\n"
    "⚙️ **Владелец бота:**\n"
    "• `рассылка [текст]` | `скачатьбд` | `выдатьденьги [юзер] [сумма]`"
)

MOD_HELP_TEXT = (
    "🛡 **Команды модерации и защиты (в беседах):**\n"
    "• `/антиреклама вкл`, `/антикапс вкл`, `/антиспам вкл`, `/антифлуд вкл`, `/антимат вкл`, `/антибот вкл`\n"
    "• `мут @юзер 15 причина` | `размут @юзер` | `варн @юзер причина` | `разварн @юзер`\n"
    "• `кик @юзер причина` | `бан @юзер причина` | `разбан ID` | `чистка 20` | `закреп`\n"
    "• `чат лок` / `чат анлок` | `ранг @юзер 50`\n"
    "• `/кмд мут 60` — поменять ранг команды ТОЛЬКО в этой беседе (нужен 100 lvl)"
)


def collect_passive_income(econ: Dict[str, Any]) -> int:
    now = int(time.time())
    last = int(econ.get("last_collect", now))
    hours = (now - last) / 3600.0
    if hours <= 0:
        return 0
    hours = min(hours, 24.0)
    hourly_total = 0
    for biz_id, lvl in econ.get("businesses", {}).items():
        if biz_id in BUSINESS_CATALOG:
            hourly_total += BUSINESS_CATALOG[biz_id]["income"] * int(lvl)
    return int(hourly_total * hours)


def parse_bet(econ: Dict[str, Any], bet_str: str) -> Optional[int]:
    s = bet_str.strip().lower()
    if s in ("все", "всё", "all", "вабанк"):
        return econ["balance"] if econ["balance"] > 0 else None
    if s.isdigit():
        val = int(s)
        return val if val > 0 else None
    return None


async def show_stats(msg: Message, target_id: int):
    rank = get_rank(msg.peer_id, target_id)
    econ = get_econ(target_id)
    name = await get_user_name(target_id)
    warns = 0
    rep = 0
    nick = "Не установлен"
    spouse_str = "Нет"
    if msg.peer_id > 2000000000:
        chat = get_chat(msg.peer_id)
        warns = int(chat["warns"].get(str(target_id), 0))
        rep = int(chat["rep"].get(str(target_id), 0))
        nick = chat["nicks"].get(str(target_id), "Не установлен")
        spouse_id = chat.get("marriages", {}).get(str(target_id))
        if spouse_id:
            spouse_str = await get_user_name(int(spouse_id))
    await msg.answer(
        f"📊 **Профиль:** {name}\n"
        f"👑 Ранг: **{rank} lvl**\n"
        f"🏷 Ник в чате: **{nick}**\n"
        f"💍 Брак: {spouse_str}\n"
        f"⭐ Репутация: **{rep}** | ⚠️ Варны: **{warns}/3**\n"
        f"💵 Баланс: **{fmt_num(econ['balance'])} $**"
    )


async def send_guard_status(msg: Message):
    if msg.peer_id <= 2000000000:
        await msg.answer("❌ Модули защиты настраиваются внутри беседы.")
        return
    chat = get_chat(msg.peer_id)
    st = lambda k: "🟢 ВКЛ" if chat.get(k) else "🔴 ВЫКЛ"
    await msg.answer(
        "🚨 **Состояние защиты беседы:**\n"
        f"• `/антиреклама` (ссылки, инвайты, @упоминания): **{st('antilink')}**\n"
        f"• `/антикапс` (КАПС): **{st('anticaps')}**\n"
        f"• `/антиспам` (повторы): **{st('antispam')}**\n"
        f"• `/антифлуд` (частые сообщения): **{st('antiflood')}**\n"
        f"• `/антимат` (грубый мат): **{st('antiswear')}**\n"
        f"• `/антибот` (автокик ботов при входе): **{st('antibot')}**\n\n"
        "Переключение: `/антиреклама вкл`, `/антикапс выкл` или `защита антиспам вкл`"
    )


async def send_cmd_ranks_list(msg: Message):
    if msg.peer_id <= 2000000000:
        await msg.answer("❌ Просмотр и смена рангов команд беседы работает внутри беседы.")
        return
    chat = get_chat(msg.peer_id)
    custom = chat.get("cmd_ranks", {})
    lines = ["🔐 **Ранги команд ТОЛЬКО в этой беседе (смена через `/кмд [команда] [ранг]` с 100 lvl):**"]
    for k, def_v in sorted(DEFAULT_CMD_RANKS.items()):
        cur_v = 100 if k == "кмд" else int(custom.get(k, def_v))
        mark = " *(изменено в чате)*" if (k in custom and k != "кмд") else ""
        lines.append(f"• `{k}` — **{cur_v} lvl**{mark}")
    await msg.answer("\n".join(lines))


async def send_reports_list(msg: Message):
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "репорты")
    if not ok:
        await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
        return
    open_reps = [r for r in db["reports"] if r.get("status") == "open"]
    if not open_reps:
        await msg.answer("✅ Открытых репортов сейчас нет.")
        return
    lines = ["📩 **Открытые репорты:**"]
    for r in open_reps[-15:]:
        lines.append(
            f"• **#{r['id']}** от [id{r['user_id']}|игрока] ({r.get('chat_title', 'чат')}): {r['text']}"
        )
    lines.append("\nОтветить из любой беседы: `ответ [ID] [текст ответа]`")
    await msg.answer("\n".join(lines))


async def send_db_dump(msg: Message):
    if not is_owner(msg.from_id):
        await msg.answer("⛔ Команда доступна только владельцу бота.")
        return
    summary = (
        f"💾 **Сводка базы данных GAMES | Чат Менеджер**\n"
        f"• Активных бесед: **{len(db['chats'])}**\n"
        f"• Игроков в экономике: **{len(db['economy'])}**\n"
        f"• Всего репортов: **{len(db['reports'])}**"
    )
    raw = json.dumps(db, ensure_ascii=False, indent=2)
    if len(raw) <= 3500:
        await msg.answer(f"{summary}\n\n```json\n{raw}\n```")
    else:
        await msg.answer(summary)
        for i in range(0, len(raw), 3800):
            await msg.answer(raw[i:i + 3800])
            await asyncio.sleep(0.1)


# ==============================================================================
# ЕДИНЫЙ РОУТЕР СООБЩЕНИЙ И КНОПОК (С ИЛИ БЕЗ `/`)
# ==============================================================================
@bot.on.message()
async def universal_message_router(msg: Message):
    if not msg.from_id:
        return

    peer_id = msg.peer_id
    user_id = msg.from_id
    is_chat = peer_id > 2000000000

    if is_chat:
        blocked = await run_chat_guard(msg)
        if blocked:
            return

    if user_id < 0:
        return

    if msg.payload:
        try:
            pl = json.loads(msg.payload) if isinstance(msg.payload, str) else msg.payload
        except Exception:
            pl = {}
        if isinstance(pl, dict):
            if pl.get("command") == "start":
                await msg.answer(
                    f"✅ Бот **GAMES | Чат Менеджер** онлайн!\n"
                    f"Ваш ранг: **{get_rank(peer_id, user_id)} lvl**\n"
                    f"Выберите раздел кнопками или напишите `помощь` / `/помощь`:",
                    keyboard=build_start_keyboard(user_id),
                )
                return
            action = pl.get("cmd")
            if action == "help":
                await msg.answer(HELP_TEXT, keyboard=build_panel_keyboard(user_id, is_chat))
                return
            if action == "panel":
                await msg.answer(
                    "⚙️ **Панель управления GAMES | Чат Менеджер**",
                    keyboard=build_panel_keyboard(user_id, is_chat),
                )
                return
            if action == "mod_help":
                await msg.answer(MOD_HELP_TEXT, keyboard=build_panel_keyboard(user_id, is_chat))
                return
            if action == "guard_status":
                await send_guard_status(msg)
                return
            if action == "cmd_ranks_list":
                await send_cmd_ranks_list(msg)
                return
            if action == "games":
                econ = get_econ(user_id)
                await msg.answer(
                    f"🎮 **Игровой центр**\n"
                    f"💰 Ваш баланс: **{fmt_num(econ['balance'])} $**\n\n"
                    f"• `бонус` — ежедневная награда\n"
                    f"• `работа` — подработка\n"
                    f"• `колесо 500` — Колесо Фортуны\n"
                    f"• `казино 500` — рулетка ставок\n"
                    f"• `слоты 500` — автоматы\n"
                    f"• `кубик 500` — кости с ботом\n"
                    f"• `бизнесы` — покупка и доход с бизнеса"
                )
                return
            if action == "stats":
                await show_stats(msg, user_id)
                return
            if action == "reports":
                await send_reports_list(msg)
                return
            if action == "dump_db":
                await send_db_dump(msg)
                return

    text = normalize_chat_text(msg.text or "")
    if not text:
        return

    low = text.lower()
    parts = split_tokens(text)
    raw_cmd = parts[0].lower() if parts else ""
    cmd = raw_cmd.lstrip("/")
    rest = text[len(parts[0]):].strip() if len(parts) > 1 else ""

    if low in ("начать", "/start", "старт", "/старт", "start"):
        await msg.answer(
            f"✅ Бот **GAMES | Чат Менеджер** онлайн!\n"
            f"Ваш ранг: **{get_rank(peer_id, user_id)} lvl**\n"
            f"Выберите действие на кнопках или напишите `помощь`:",
            keyboard=build_start_keyboard(user_id),
        )
        return

    if low in ("/статус", "статус", "/status", "status"):
        await msg.answer(
            f"🟢 **Статус: ONLINE**\n"
            f"• Бесед в базе: **{len(db['chats'])}**\n"
            f"• Игроков в экономике: **{len(db['economy'])}**\n"
            f"• Ваш ранг: **{get_rank(peer_id, user_id)} lvl**",
            keyboard=build_start_keyboard(user_id),
        )
        return

    if low in ("/помощь", "помощь", "/help", "help", "/хелп", "/команды", "команды", "хэлп", "/хэлп"):
        await msg.answer(HELP_TEXT, keyboard=build_panel_keyboard(user_id, is_chat))
        return

    if low in ("/панель", "панель", "/панелл", "панелл", "/menu", "/panel", "меню"):
        await msg.answer(
            "⚙️ **Панель управления GAMES | Чат Менеджер**",
            keyboard=build_panel_keyboard(user_id, is_chat),
        )
        return

    # --- КОМАНДА /кмд (100 LVL, МЕНЯЕТ РАНГ ЛЮБОЙ КОМАНДЫ ТОЛЬКО В ЭТОЙ БЕСЕДЕ) ---
    if cmd in ("кмд", "кдм"):
        if not is_chat:
            await msg.answer("❌ Команда `/кмд` работает только внутри беседы.")
            return
        ok, req = has_cmd_access(peer_id, user_id, "кмд")
        if not ok:
            await msg.answer(f"⛔ Команда `/кмд` доступна строго с **{req} lvl**.")
            return
        if len(parts) < 3 or not parts[2].isdigit():
            avail = ", ".join(f"`{k}`" for k in sorted(DEFAULT_CMD_RANKS.keys()) if k != "кмд")
            await msg.answer(
                "⚙️ **Смена ранга команды ТОЛЬКО в этой беседе (100 lvl):**\n"
                "Формат: `/кмд [команда] [ранг 0-100]`\n"
                "Пример: `/кмд мут 50` или `/кмд антиреклама 70`\n\n"
                f"Команды: {avail}"
            )
            return
        target_cmd = parts[1].strip().lower().lstrip("/")
        if target_cmd == "кмд":
            await msg.answer("❌ Ранг команды `/кмд` фиксирован на **100 lvl**.")
            return
        if target_cmd not in DEFAULT_CMD_RANKS:
            await msg.answer(f"❌ Команда `{target_cmd}` не найдена.")
            return
        new_lvl = int(parts[2])
        if not (0 <= new_lvl <= 100):
            await msg.answer("❌ Ранг должен быть от `0` до `100`.")
            return
        chat = get_chat(peer_id)
        chat["cmd_ranks"][target_cmd] = new_lvl
        save_db(db)
        await msg.answer(
            f"✅ В **этой беседе** для команды `{target_cmd}` установлен ранг **{new_lvl} lvl**!"
        )
        return

    if cmd == "доступ":
        if not is_chat:
            await msg.answer("❌ Настройка прав команд работает только внутри беседы.")
            return
        ok, req = has_cmd_access(peer_id, user_id, "доступ")
        if not ok:
            await msg.answer(f"⛔ Эта команда доступна с **{req}+ lvl**.")
            return
        if len(parts) < 3 or not parts[2].isdigit():
            await msg.answer("❌ Формат: `/доступ [команда] [ранг 0-100]`")
            return
        target_cmd = parts[1].strip().lower().lstrip("/")
        if target_cmd == "кмд":
            await msg.answer("❌ Ранг команды `/кмд` фиксирован на **100 lvl**.")
            return
        if target_cmd not in DEFAULT_CMD_RANKS:
            await msg.answer(f"❌ Команда `{target_cmd}` не найдена.")
            return
        new_lvl = int(parts[2])
        if not (0 <= new_lvl <= 100):
            await msg.answer("❌ Ранг должен быть от `0` до `100`.")
            return
        chat = get_chat(peer_id)
        chat["cmd_ranks"][target_cmd] = new_lvl
        save_db(db)
        await msg.answer(f"✅ В **этой беседе** команда `{target_cmd}` теперь с **{new_lvl} lvl**!")
        return

    if cmd in ("доступы", "права"):
        await send_cmd_ranks_list(msg)
        return

    if cmd in ("репорт", "report"):
        if not rest:
            await msg.answer("❌ Напишите текст репорта: `репорт [описание проблемы]`")
            return
        rep_id = len(db["reports"]) + 1
        chat_title = "Личные сообщения"
        if is_chat:
            chat_title = get_chat(peer_id).get("title", f"Чат {peer_id}")
        report_obj = {
            "id": rep_id,
            "peer_id": peer_id,
            "chat_title": chat_title,
            "user_id": user_id,
            "text": rest,
            "status": "open",
            "answer": None,
            "answered_by": None,
            "ts": int(time.time()),
        }
        db["reports"].append(report_obj)
        save_db(db)
        sender_name = await get_user_name(user_id)
        await msg.answer(f"✅ **Репорт #{rep_id} принят!**\nОжидайте ответа администрации прямо в этот чат.")
        alert = (
            f"🚨 **Новый репорт #{rep_id}**\n"
            f"👤 От: {sender_name}\n"
            f"💬 Беседа: {chat_title} (`{peer_id}`)\n"
            f"📝 Текст: {rest}\n\n"
            f"👉 Ответить из любого чата: `ответ {rep_id} ваш_текст`"
        )
        for oid in OWNER_IDS:
            try:
                await bot.api.messages.send(peer_id=oid, message=alert, random_id=random.randint(1, 2**31 - 1))
            except Exception:
                pass
        return

    if cmd in ("репорты", "reports"):
        await send_reports_list(msg)
        return

    if cmd in ("ответ", "ответить"):
        ok, req = has_cmd_access(peer_id, user_id, "ответ")
        if not ok:
            await msg.answer(f"⛔ Отвечать на репорты можно с **{req}+ lvl**.")
            return
        if len(parts) < 3 or not parts[1].isdigit():
            await msg.answer("❌ Формат: `ответ [ID] [текст ответа]`")
            return
        rep_id = int(parts[1])
        ans_text = text[len(parts[0]) + len(parts[1]):].strip()
        if not ans_text:
            await msg.answer("❌ Напишите текст ответа после ID репорта.")
            return
        target_rep = next((r for r in db["reports"] if r["id"] == rep_id), None)
        if not target_rep:
            await msg.answer(f"❌ Репорт #{rep_id} не найден.")
            return
        admin_name = await get_user_name(user_id)
        u_name = await get_user_name(int(target_rep["user_id"]))
        origin_peer_id = int(target_rep["peer_id"])
        target_rep["status"] = "closed"
        target_rep["answer"] = ans_text
        target_rep["answered_by"] = user_id
        save_db(db)
        delivery_text = (
            f"📨 **Ответ на ваш репорт #{rep_id}**\n"
            f"👤 Игрок: {u_name}\n"
            f"❓ Ваш вопрос: {target_rep['text']}\n"
            f"🛡 Администратор: {admin_name}\n"
            f"💬 Ответ: **{ans_text}**"
        )
        try:
            await bot.api.messages.send(
                peer_id=origin_peer_id,
                message=delivery_text,
                random_id=random.randint(1, 2**31 - 1),
            )
            await msg.answer(
                f"✅ Ответ на репорт **#{rep_id}** отправлен в исходный чат (`{target_rep.get('chat_title', origin_peer_id)}`)!"
            )
        except Exception as e:
            await msg.answer(f"⚠️ Статус обновлён, но не удалось отправить в исходный чат: `{e}`")
        return

    if cmd in ("баланс", "б"):
        econ = get_econ(user_id)
        pending = collect_passive_income(econ)
        await msg.answer(
            f"💳 **Финансы {await get_user_name(user_id)}**\n"
            f"💵 На руках: **{fmt_num(econ['balance'])} $**\n"
            f"🏢 Накоплено в бизнесах: **{fmt_num(pending)} $** (`собрать`)"
        )
        return

    if cmd == "бонус":
        econ = get_econ(user_id)
        now = int(time.time())
        cd = 12 * 3600
        diff = now - int(econ.get("last_bonus", 0))
        if diff < cd:
            rem = cd - diff
            await msg.answer(f"⏳ Бонус уже получен! Следующий через **{rem // 3600} ч. {(rem % 3600) // 60} мин.**")
            return
        prize = random.randint(1500, 5000)
        econ["balance"] += prize
        econ["last_bonus"] = now
        save_db(db)
        await msg.answer(f"🎁 Вы забрали ежедневный бонус: **+{fmt_num(prize)} $**!\n💵 Баланс: **{fmt_num(econ['balance'])} $**")
        return

    if cmd == "работа":
        econ = get_econ(user_id)
        now = int(time.time())
        cd = 15 * 60
        diff = now - int(econ.get("last_work", 0))
        if diff < cd:
            await msg.answer(f"⏳ Вы устали! Отдохните ещё **{(cd - diff) // 60 + 1} мин.**")
            return
        jobs = [
            ("настроили сервер CRMP", random.randint(600, 1400)),
            ("пофиксили баги в коде бота", random.randint(800, 1800)),
            ("развезли заказы по городу", random.randint(500, 1200)),
            ("провели ивент для игроков", random.randint(700, 1600)),
        ]
        job_desc, pay = random.choice(jobs)
        econ["balance"] += pay
        econ["last_work"] = now
        save_db(db)
        await msg.answer(f"🛠 Вы {job_desc} и заработали **+{fmt_num(pay)} $**!\n💵 Баланс: **{fmt_num(econ['balance'])} $**")
        return

    if cmd == "перевод":
        if len(parts) < 3 or not parts[2].isdigit():
            await msg.answer("❌ Использование: `перевод @пользователь сумма`")
            return
        tid = parse_target_id(msg, parts[1])
        amount = int(parts[2])
        if not tid or tid == user_id or amount <= 0:
            await msg.answer("❌ Неверный получатель или сумма.")
            return
        sender = get_econ(user_id)
        if sender["balance"] < amount:
            await msg.answer("❌ Недостаточно средств на балансе.")
            return
        receiver = get_econ(tid)
        sender["balance"] -= amount
        receiver["balance"] += amount
        save_db(db)
        await msg.answer(f"💸 Перевод **{fmt_num(amount)} $** игроку {await get_user_name(tid)} выполнен!")
        return

    if cmd in ("казино", "каз"):
        econ = get_econ(user_id)
        bet = parse_bet(econ, parts[1] if len(parts) > 1 else "")
        if not bet:
            await msg.answer("❌ Укажите ставку: `казино 500` или `казино все`")
            return
        if econ["balance"] < bet:
            await msg.answer(f"❌ Недостаточно средств! Баланс: **{fmt_num(econ['balance'])} $**")
            return
        roll = random.randint(1, 100)
        if roll <= 46:
            econ["balance"] -= bet
            res = f"📉 Вы проиграли **{fmt_num(bet)} $**."
        elif roll <= 90:
            econ["balance"] += bet
            res = f"🎉 Победа (x2)! Вы выиграли **+{fmt_num(bet)} $**!"
        else:
            win = bet * 4
            econ["balance"] += win
            res = f"🔥 ДЖЕКПОТ (x5)! Вы сорвали куш **+{fmt_num(win)} $**!"
        save_db(db)
        await msg.answer(f"🎰 **Казино**\n{res}\n💵 Баланс: **{fmt_num(econ['balance'])} $**")
        return

    if cmd == "колесо":
        econ = get_econ(user_id)
        bet = parse_bet(econ, parts[1] if len(parts) > 1 else "")
        if not bet:
            await msg.answer("❌ Укажите ставку: `колесо 500`")
            return
        if econ["balance"] < bet:
            await msg.answer(f"❌ Недостаточно средств! Баланс: **{fmt_num(econ['balance'])} $**")
            return
        sectors = [
            ("💀 Банкрот (0x)", 0.0, 32),
            ("🔸 Утешительный (0.5x)", 0.5, 22),
            ("🟢 Возврат (1x)", 1.0, 16),
            ("🔵 Удача (2x)", 2.0, 18),
            ("🟣 Супер-сектор (3x)", 3.0, 8),
            ("👑 МЕГА-ПРИЗ (10x)", 10.0, 4),
        ]
        idx = random.choices(range(len(sectors)), weights=[s[2] for s in sectors], k=1)[0]
        payout = int(bet * sectors[idx][1])
        econ["balance"] = econ["balance"] - bet + payout
        save_db(db)
        diff = payout - bet
        sign = f"+{fmt_num(diff)}" if diff >= 0 else f"{fmt_num(diff)}"
        await msg.answer(
            f"🎡 **Колесо Фортуны**\nСектор: **{sectors[idx][0]}**\nИтог: **{sign} $**\n💵 Баланс: **{fmt_num(econ['balance'])} $**"
        )
        return

    if cmd == "слоты":
        econ = get_econ(user_id)
        bet = parse_bet(econ, parts[1] if len(parts) > 1 else "")
        if not bet:
            await msg.answer("❌ Укажите ставку: `слоты 500`")
            return
        if econ["balance"] < bet:
            await msg.answer(f"❌ Недостаточно средств! Баланс: **{fmt_num(econ['balance'])} $**")
            return
        symbols = ["🍒", "🍋", "🍇", "💎", "7️⃣"]
        line = [random.choice(symbols) for _ in range(3)]
        display = f"[ {line[0]} | {line[1]} | {line[2]} ]"
        if line[0] == line[1] == line[2]:
            mult = 8 if line[0] == "7️⃣" else (5 if line[0] == "💎" else 3)
            win = bet * mult
            econ["balance"] += win - bet
            status = f"🔥 ТРИ В РЯД (x{mult})! Выигрыш: **+{fmt_num(win)} $**"
        elif line[0] == line[1] or line[1] == line[2] or line[0] == line[2]:
            win = int(bet * 1.5)
            econ["balance"] += win - bet
            status = f"✨ Две совпали (x1.5)! Выигрыш: **+{fmt_num(win)} $**"
        else:
            econ["balance"] -= bet
            status = f"📉 Не повезло! Проигрыш: **-{fmt_num(bet)} $**"
        save_db(db)
        await msg.answer(f"🎰 **Слоты:** {display}\n{status}\n💵 Баланс: **{fmt_num(econ['balance'])} $**")
        return

    if cmd == "кубик":
        econ = get_econ(user_id)
        bet = parse_bet(econ, parts[1] if len(parts) > 1 else "")
        if not bet:
            await msg.answer("❌ Укажите ставку: `кубик 500`")
            return
        if econ["balance"] < bet:
            await msg.answer(f"❌ Недостаточно средств! Баланс: **{fmt_num(econ['balance'])} $**")
            return
        p_roll = random.randint(1, 6)
        b_roll = random.randint(1, 6)
        if p_roll > b_roll:
            econ["balance"] += bet
            outcome = f"🎉 Вы победили! **+{fmt_num(bet)} $**"
        elif p_roll < b_roll:
            econ["balance"] -= bet
            outcome = f"📉 Бот выбросил больше! **-{fmt_num(bet)} $**"
        else:
            outcome = "🤝 Ничья! Ставка возвращена."
        save_db(db)
        await msg.answer(
            f"🎲 **Кубики**\nВы выбросили: **{p_roll}** | Бот выбросил: **{b_roll}**\n{outcome}\n💵 Баланс: **{fmt_num(econ['balance'])} $**"
        )
        return

    if cmd in ("бизнесы", "бизнес"):
        lines = ["🏢 **Каталог бизнесов** (пассивный доход каждый час):"]
        for bid, info in BUSINESS_CATALOG.items():
            lines.append(
                f"**{bid}. {info['name']}** — Цена: **{fmt_num(info['cost'])} $** | Доход: **{fmt_num(info['income'])} $/ч**"
            )
        lines.append("\nКупить или улучшить бизнес: `купитьбиз [номер]`\nЗабрать прибыль: `собрать`")
        await msg.answer("\n".join(lines))
        return

    if cmd == "купитьбиз":
        bid = parts[1].strip() if len(parts) > 1 else ""
        if bid not in BUSINESS_CATALOG:
            await msg.answer("❌ Неверный номер бизнеса. Посмотрите `бизнесы`.")
            return
        econ = get_econ(user_id)
        pending = collect_passive_income(econ)
        econ["balance"] += pending
        econ["last_collect"] = int(time.time())
        cur_lvl = int(econ["businesses"].get(bid, 0))
        info = BUSINESS_CATALOG[bid]
        price = info["cost"] * (cur_lvl + 1)
        if econ["balance"] < price:
            await msg.answer(
                f"❌ Для покупки/улучшения «{info['name']}» (ур. {cur_lvl + 1}) нужно **{fmt_num(price)} $**.\nВаш баланс: **{fmt_num(econ['balance'])} $**"
            )
            return
        econ["balance"] -= price
        econ["businesses"][bid] = cur_lvl + 1
        save_db(db)
        await msg.answer(
            f"✅ Вы приобрели/улучшили **{info['name']}** до **{cur_lvl + 1} ур.** за **{fmt_num(price)} $**!\n📈 Новый доход: **{fmt_num(info['income'] * (cur_lvl + 1))} $/ч**"
        )
        return

    if cmd in ("мойбиз", "моибизнесы"):
        econ = get_econ(user_id)
        if not econ["businesses"]:
            await msg.answer("🏢 У вас пока нет бизнесов. Откройте `бизнесы` и купите первый!")
            return
        lines = ["🏢 **Ваши бизнесы:**"]
        total_hr = 0
        for bid, lvl in econ["businesses"].items():
            if bid in BUSINESS_CATALOG:
                inc = BUSINESS_CATALOG[bid]["income"] * int(lvl)
                total_hr += inc
                lines.append(f"• **{BUSINESS_CATALOG[bid]['name']}** ({lvl} ур.) — **{fmt_num(inc)} $/ч**")
        pending = collect_passive_income(econ)
        lines.append(f"\n📊 Общий доход: **{fmt_num(total_hr)} $/ч**")
        lines.append(f"💰 Накоплено в кассе: **{fmt_num(pending)} $** (`собрать`)")
        await msg.answer("\n".join(lines))
        return

    if cmd == "собрать":
        econ = get_econ(user_id)
        earned = collect_passive_income(econ)
        if earned <= 0:
            await msg.answer("📭 В кассе бизнесов пока пусто. Приходите чуть позже!")
            return
        econ["balance"] += earned
        econ["last_collect"] = int(time.time())
        save_db(db)
        await msg.answer(f"💰 Вы собрали прибыль с бизнесов: **+{fmt_num(earned)} $**!\n💵 Баланс: **{fmt_num(econ['balance'])} $**")
        return

    if cmd == "рассылка":
        if not is_owner(user_id):
            await msg.answer("⛔ Рассылка доступна **только владельцу бота**!")
            return
        if not rest:
            await msg.answer("❌ Напишите текст рассылки: `рассылка текст`")
            return
        sent_ok = 0
        sent_err = 0
        broadcast_msg = f"📢 **Объявление от администрации GAMES:**\n\n{rest}"
        for pid_str in list(db["chats"].keys()):
            pid = int(pid_str)
            try:
                await bot.api.messages.send(
                    peer_id=pid,
                    message=broadcast_msg,
                    random_id=random.randint(1, 2**31 - 1),
                )
                sent_ok += 1
                await asyncio.sleep(0.05)
            except Exception:
                sent_err += 1
        await msg.answer(
            f"✅ **Рассылка завершена!**\n📨 Доставлено в бесед: **{sent_ok}**\n⚠️ Ошибок отправки: **{sent_err}**"
        )
        return

    if cmd == "выдатьденьги":
        if not is_owner(user_id):
            return
        if len(parts) < 3 or not parts[2].isdigit():
            await msg.answer("❌ Использование: `выдатьденьги @юзер сумма`")
            return
        tid = parse_target_id(msg, parts[1])
        if not tid:
            await msg.answer("❌ Укажите пользователя.")
            return
        amount = int(parts[2])
        econ = get_econ(tid)
        econ["balance"] += amount
        save_db(db)
        await msg.answer(f"💸 Выдано **{fmt_num(amount)} $** игроку {await get_user_name(tid)}.")
        return

    if cmd in ("скачатьбд", "бекапбд", "бэкапбд", "дампбд"):
        await send_db_dump(msg)
        return

    if cmd in ("стата", "профиль"):
        tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None) or user_id
        await show_stats(msg, tid)
        return

    if cmd == "ник":
        if not is_chat:
            await msg.answer("❌ Команда работает только в беседах.")
            return
        ok, req = has_cmd_access(peer_id, user_id, "ник")
        if not ok:
            await msg.answer(f"⛔ Команда `ник` доступна с **{req}+ lvl**.")
            return
        if not rest:
            await msg.answer("❌ Укажите ник: `ник НовыйНик`")
            return
        clean_nick = rest[:32]
        chat = get_chat(peer_id)
        chat["nicks"][str(user_id)] = clean_nick
        save_db(db)
        await msg.answer(f"🏷 Ваш новый ник в этой беседе: **{clean_nick}**")
        return

    if cmd == "сник":
        if not is_chat:
            await msg.answer("❌ Команда работает только в беседах.")
            return
        ok, req = has_cmd_access(peer_id, user_id, "сник")
        if not ok:
            await msg.answer(f"⛔ Команда `сник` в этой беседе доступна с **{req}+ lvl**.")
            return
        tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
        if msg.reply_message and msg.reply_message.from_id:
            tid = msg.reply_message.from_id
            new_nick = rest
        else:
            new_nick = " ".join(parts[2:]).strip()
        if not tid or not new_nick:
            await msg.answer("❌ Использование: `сник @юзер НовыйНик` (или ответом `сник НовыйНик`)")
            return
        chat = get_chat(peer_id)
        chat["nicks"][str(tid)] = new_nick[:32]
        save_db(db)
        await msg.answer(f"🏷 Игроку {await get_user_name(tid)} установлен ник: **{new_nick[:32]}**")
        return

    if cmd == "брак":
        if not is_chat:
            await msg.answer("❌ Браки доступны только в беседах.")
            return
        tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
        if not tid or tid == user_id or tid < 0:
            await msg.answer("❌ Укажите игрока: `брак @пользователь` (или ответом)")
            return
        chat = get_chat(peer_id)
        marriages = chat.setdefault("marriages", {})
        if str(user_id) in marriages or str(tid) in marriages:
            await msg.answer("💔 Кто-то из вас уже состоит в браке в этой беседе!")
            return
        marriages[str(user_id)] = tid
        marriages[str(tid)] = user_id
        save_db(db)
        await msg.answer(f"💍 Поздравляем! {await get_user_name(user_id)} и {await get_user_name(tid)} теперь в браке!")
        return

    if cmd == "развод":
        if not is_chat:
            return
        chat = get_chat(peer_id)
        marriages = chat.setdefault("marriages", {})
        spouse = marriages.pop(str(user_id), None)
        if not spouse:
            await msg.answer("❌ Вы не состоите в браке.")
            return
        marriages.pop(str(spouse), None)
        save_db(db)
        await msg.answer(f"💔 Брак между {await get_user_name(user_id)} и {await get_user_name(int(spouse))} расторгнут.")
        return

    if cmd == "шар":
        answers = [
            "Бесспорно, да! ✅",
            "Скорее всего, да 👍",
            "Звёзды говорят — нет ❌",
            "Спроси позже, сейчас туманно 🔮",
            "100% инфа, так и будет 🔥",
            "Даже не надейся 😅",
        ]
        await msg.answer(f"🎱 **Шар судьбы:** {random.choice(answers)}")
        return

    if cmd == "выбор":
        opts = [p.strip() for p in re.split(r"\s+или\s+|,", rest, flags=re.I) if p.strip()]
        if len(opts) < 2:
            await msg.answer("❌ Напишите минимум 2 варианта через «или»: `выбор чай или кофе`")
            return
        await msg.answer(f"🎯 Я выбираю: **{random.choice(opts)}**")
        return

    if low in ("+реп", "-реп"):
        if not is_chat or not msg.reply_message or not msg.reply_message.from_id:
            return
        tid = msg.reply_message.from_id
        if tid == user_id or tid < 0:
            return
        chat = get_chat(peer_id)
        delta = 1 if low == "+реп" else -1
        cur = int(chat["rep"].get(str(tid), 0)) + delta
        chat["rep"][str(tid)] = cur
        save_db(db)
        await msg.answer(f"{'👍' if delta > 0 else '👎'} Репутация {await get_user_name(tid)} изменена! Текущая: **{cur}**")
        return

    if cmd == "топ":
        if not is_chat:
            return
        chat = get_chat(peer_id)
        if not chat["rep"]:
            await msg.answer("🏆 Рейтинг репутации пока пуст.")
            return
        top_list = sorted(chat["rep"].items(), key=lambda x: int(x[1]), reverse=True)[:10]
        lines = ["🏆 **Топ участников по репутации:**"]
        for idx, (uid, val) in enumerate(top_list, 1):
            lines.append(f"{idx}. {await get_user_name(int(uid))} — **{val}**")
        await msg.answer("\n".join(lines))
        return

    # --- МОДЕРАЦИЯ И ЗАЩИТА В БЕСЕДАХ (С ИЛИ БЕЗ `/`) ---
    if not is_chat:
        return

    guard_cmd_map = {
        "антиспам": ("antispam", "Антиспам"),
        "антикапс": ("anticaps", "Антикапс"),
        "антифлуд": ("antiflood", "Антифлуд"),
        "антиреклама": ("antilink", "Антиреклама"),
        "антиссылки": ("antilink", "Антиреклама"),
        "антимат": ("antiswear", "Антимат"),
        "антибот": ("antibot", "Антибот"),
    }
    if cmd in guard_cmd_map:
        prop_key, label_ru = guard_cmd_map[cmd]
        ok, req = has_cmd_access(peer_id, user_id, cmd)
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        chat = get_chat(peer_id)
        new_state = parse_toggle_arg(parts[1] if len(parts) > 1 else None, bool(chat.get(prop_key, False)))
        chat[prop_key] = new_state
        save_db(db)
        await msg.answer(f"🚨 **{label_ru}** теперь **{'ВКЛЮЧЕН' if new_state else 'ВЫКЛЮЧЕН'}** в этой беседе.")
        return

    if cmd in ("защиты", "статусзащиты"):
        await send_guard_status(msg)
        return

    if cmd == "защита":
        ok, req = has_cmd_access(peer_id, user_id, "защита")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if len(parts) < 2:
            await send_guard_status(msg)
            return
        mapping = {
            "антиспам": ("antispam", "Антиспам"),
            "антикапс": ("anticaps", "Антикапс"),
            "антифлуд": ("antiflood", "Антифлуд"),
            "антиреклама": ("antilink", "Антиреклама"),
            "антиссылки": ("antilink", "Антиреклама"),
            "антимат": ("antiswear", "Антимат"),
            "антибот": ("antibot", "Антибот"),
        }
        entry = mapping.get(parts[1].lower())
        if not entry:
            await msg.answer("❌ Модули: `антиспам`, `антикапс`, `антифлуд`, `антиреклама`, `антимат`, `антибот`")
            return
        key, label_ru = entry
        chat = get_chat(peer_id)
        enabled = parse_toggle_arg(parts[2] if len(parts) > 2 else None, bool(chat.get(key, False)))
        chat[key] = enabled
        save_db(db)
        await msg.answer(f"🛡 Модуль **{label_ru}** теперь **{'ВКЛЮЧЕН' if enabled else 'ВЫКЛЮЧЕН'}**.")
        return

    if cmd == "ранг":
        ok, req = has_cmd_access(peer_id, user_id, "ранг")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if msg.reply_message and msg.reply_message.from_id:
            tid = msg.reply_message.from_id
            lvl_str = parts[1] if len(parts) > 1 else ""
        else:
            tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
            lvl_str = parts[2] if len(parts) > 2 else ""
        if not tid or not lvl_str.isdigit():
            await msg.answer("❌ Использование: `ранг @пользователь 50` (или ответом `ранг 50`)")
            return
        new_lvl = int(lvl_str)
        if not (0 <= new_lvl <= 100):
            await msg.answer("❌ Ранг должен быть от 0 до 100.")
            return
        caller_rank = get_rank(peer_id, user_id)
        if new_lvl >= caller_rank and not is_owner(user_id):
            await msg.answer("⛔ Нельзя выдать ранг выше или равный своему.")
            return
        chat = get_chat(peer_id)
        chat["ranks"][str(tid)] = new_lvl
        save_db(db)
        await msg.answer(f"✅ Игроку {await get_user_name(tid)} установлен ранг **{new_lvl} lvl**.")
        return

    if cmd == "админы":
        chat = get_chat(peer_id)
        lines = ["👑 **Администрация беседы:**"]
        found = False
        for uid_str, lvl in sorted(chat["ranks"].items(), key=lambda x: int(x[1]), reverse=True):
            if int(lvl) > 0:
                found = True
                lines.append(f"• {await get_user_name(int(uid_str))} — **{lvl} lvl**")
        if not found:
            lines.append("Список локальных админов пуст.")
        await msg.answer("\n".join(lines))
        return

    if cmd == "аварн":
        ok, req = has_cmd_access(peer_id, user_id, "аварн")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if msg.reply_message and msg.reply_message.from_id:
            tid = msg.reply_message.from_id
            reason = rest or "Нарушение регламента админов"
        else:
            tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
            reason = " ".join(parts[2:]).strip() or "Нарушение регламента админов"
        if not tid:
            await msg.answer("❌ Укажите администратора: `аварн @юзер причина`")
            return
        chat = get_chat(peer_id)
        aw = int(chat["admin_warns"].get(str(tid), 0)) + 1
        if aw >= 3:
            chat["admin_warns"][str(tid)] = 0
            chat["ranks"][str(tid)] = 0
            save_db(db)
            await msg.answer(f"🔻 Администратор {await get_user_name(tid)} получил **3/3 выговора** и был снят с поста!")
        else:
            chat["admin_warns"][str(tid)] = aw
            save_db(db)
            await msg.answer(f"📛 Администратору {await get_user_name(tid)} выдан выговор (**{aw}/3**).\nПричина: {reason}")
        return

    if cmd == "снятьаварн":
        ok, req = has_cmd_access(peer_id, user_id, "снятьаварн")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
        if not tid:
            await msg.answer("❌ Укажите администратора.")
            return
        chat = get_chat(peer_id)
        aw = max(0, int(chat["admin_warns"].get(str(tid), 0)) - 1)
        chat["admin_warns"][str(tid)] = aw
        save_db(db)
        await msg.answer(f"✅ Снят 1 админ-выговор у {await get_user_name(tid)} (**{aw}/3**).")
        return

    if cmd == "мут":
        ok, req = has_cmd_access(peer_id, user_id, "мут")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if msg.reply_message and msg.reply_message.from_id:
            tid = msg.reply_message.from_id
            mins_str = parts[1] if len(parts) > 1 else ""
            reason = " ".join(parts[2:]).strip() or "Нарушение правил"
        else:
            tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
            mins_str = parts[2] if len(parts) > 2 else ""
            reason = " ".join(parts[3:]).strip() or "Нарушение правил"
        if not tid or not mins_str.isdigit():
            await msg.answer("❌ Формат: `мут @юзер 15 причина` (или ответом `мут 15 причина`)")
            return
        mins = int(mins_str)
        chat = get_chat(peer_id)
        chat["mutes"][str(tid)] = time.time() + mins * 60
        save_db(db)
        await msg.answer(f"🔇 {await get_user_name(tid)} получил мут на **{mins} мин.**\nПричина: {reason}")
        return

    if cmd == "размут":
        ok, req = has_cmd_access(peer_id, user_id, "размут")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
        if not tid:
            await msg.answer("❌ Укажите игрока.")
            return
        chat = get_chat(peer_id)
        chat["mutes"].pop(str(tid), None)
        save_db(db)
        await msg.answer(f"🔊 Мут с {await get_user_name(tid)} снят.")
        return

    if cmd == "варн":
        ok, req = has_cmd_access(peer_id, user_id, "варн")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if msg.reply_message and msg.reply_message.from_id:
            tid = msg.reply_message.from_id
            reason = rest or "Нарушение правил"
        else:
            tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
            reason = " ".join(parts[2:]).strip() or "Нарушение правил"
        if not tid:
            await msg.answer("❌ Формат: `варн @юзер причина`")
            return
        chat = get_chat(peer_id)
        w = int(chat["warns"].get(str(tid), 0)) + 1
        if w >= 3:
            chat["warns"][str(tid)] = 0
            save_db(db)
            try:
                await bot.api.messages.remove_chat_user(chat_id=peer_id - 2000000000, member_id=tid)
                await msg.answer(f"⛔ {await get_user_name(tid)} получил **3/3 варна** и был исключён!")
            except Exception:
                await msg.answer(f"⚠️ {await get_user_name(tid)} набрал **3/3 варна**, но у бота нет прав кикнуть его.")
        else:
            chat["warns"][str(tid)] = w
            save_db(db)
            await msg.answer(f"⚠️ {await get_user_name(tid)} выдано предупреждение (**{w}/3**).\nПричина: {reason}")
        return

    if cmd == "разварн":
        ok, req = has_cmd_access(peer_id, user_id, "разварн")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
        if not tid:
            return
        chat = get_chat(peer_id)
        w = max(0, int(chat["warns"].get(str(tid), 0)) - 1)
        chat["warns"][str(tid)] = w
        save_db(db)
        await msg.answer(f"✅ Снят 1 варн у {await get_user_name(tid)}. Текущие варны: **{w}/3**.")
        return

    if cmd == "кик":
        ok, req = has_cmd_access(peer_id, user_id, "кик")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if msg.reply_message and msg.reply_message.from_id:
            tid = msg.reply_message.from_id
            reason = rest or "Решение администрации"
        else:
            tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
            reason = " ".join(parts[2:]).strip() or "Решение администрации"
        if not tid:
            await msg.answer("❌ Формат: `кик @юзер причина`")
            return
        try:
            await bot.api.messages.remove_chat_user(chat_id=peer_id - 2000000000, member_id=tid)
            await msg.answer(f"👢 {await get_user_name(tid)} исключён из беседы.\nПричина: {reason}")
        except Exception as e:
            await msg.answer(f"❌ Не удалось кикнуть (проверьте права администратора у бота): `{e}`")
        return

    if cmd == "бан":
        ok, req = has_cmd_access(peer_id, user_id, "бан")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if msg.reply_message and msg.reply_message.from_id:
            tid = msg.reply_message.from_id
            reason = rest or "Бан в беседе"
        else:
            tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
            reason = " ".join(parts[2:]).strip() or "Бан в беседе"
        if not tid:
            await msg.answer("❌ Формат: `бан @юзер причина`")
            return
        chat = get_chat(peer_id)
        chat["bans"][str(tid)] = reason
        save_db(db)
        try:
            await bot.api.messages.remove_chat_user(chat_id=peer_id - 2000000000, member_id=tid)
        except Exception:
            pass
        await msg.answer(f"🔨 {await get_user_name(tid)} заблокирован в беседе.\nПричина: {reason}")
        return

    if cmd == "разбан":
        ok, req = has_cmd_access(peer_id, user_id, "разбан")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        tid = parse_target_id(msg, parts[1] if len(parts) > 1 else None)
        if not tid:
            return
        chat = get_chat(peer_id)
        chat["bans"].pop(str(tid), None)
        save_db(db)
        await msg.answer(f"🕊 Блокировка с {await get_user_name(tid)} снята.")
        return

    if cmd == "чистка":
        ok, req = has_cmd_access(peer_id, user_id, "чистка")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if len(parts) < 2 or not parts[1].isdigit():
            await msg.answer("❌ Формат: `чистка 20`")
            return
        cnt = min(max(1, int(parts[1])), 100)
        cmids = list(range(max(1, msg.conversation_message_id - cnt), msg.conversation_message_id + 1))
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=cmids, delete_for_all=True)
            await msg.answer(f"🧹 Очищено последних сообщений: **{cnt}**")
        except Exception as e:
            await msg.answer(f"❌ Не удалось удалить сообщения: `{e}`")
        return

    if cmd == "закреп":
        ok, req = has_cmd_access(peer_id, user_id, "закреп")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if not msg.reply_message:
            await msg.answer("❌ Ответьте командой `закреп` на сообщение.")
            return
        try:
            await bot.api.messages.pin(peer_id=peer_id, conversation_message_id=msg.reply_message.conversation_message_id)
            await msg.answer("📌 Сообщение закреплено!")
        except Exception as e:
            await msg.answer(f"❌ Ошибка закрепления: `{e}`")
        return

    if cmd == "чат" and len(parts) >= 2 and parts[1].lower() in ("лок", "анлок"):
        ok, req = has_cmd_access(peer_id, user_id, "чатлок")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        chat = get_chat(peer_id)
        lock = parts[1].lower() == "лок"
        chat["locked"] = lock
        save_db(db)
        await msg.answer("🔒 Чат закрыт для обычных участников." if lock else "🔓 Чат снова открыт для всех!")
        return

    if cmd == "приветствие":
        ok, req = has_cmd_access(peer_id, user_id, "приветствие")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        chat = get_chat(peer_id)
        if not rest or rest.lower() in ("выкл", "off", "удалить"):
            chat["welcome"] = ""
            save_db(db)
            await msg.answer("🔕 Приветствие новых участников отключено.")
        else:
            chat["welcome"] = rest
            save_db(db)
            await msg.answer("✅ Приветствие сохранено! Используйте `{user}` для упоминания новичка.")
        return

    if cmd == "лидер":
        ok, req = has_cmd_access(peer_id, user_id, "лидер")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if len(parts) < 2:
            await msg.answer("❌ Формат: `лидер фракция @юзер`")
            return
        faction = parts[1].strip()
        tid = parse_target_id(msg, parts[2] if len(parts) > 2 else None)
        if not tid:
            await msg.answer("❌ Укажите пользователя.")
            return
        chat = get_chat(peer_id)
        chat["leaders"][faction] = tid
        save_db(db)
        await msg.answer(f"🎖 Назначен лидер фракции **{faction}**: {await get_user_name(tid)}")
        return

    if cmd == "лидеры":
        chat = get_chat(peer_id)
        if not chat["leaders"]:
            await msg.answer("📋 Список лидеров пока пуст.")
            return
        lines = ["🎖 **Действующие лидеры:**"]
        for fac, uid in chat["leaders"].items():
            lines.append(f"• **{fac}** — {await get_user_name(int(uid))}")
        await msg.answer("\n".join(lines))
        return

    if cmd == "заметка":
        ok, req = has_cmd_access(peer_id, user_id, "заметка")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if len(parts) < 3:
            await msg.answer("❌ Формат: `заметка имя текст`")
            return
        note_name = parts[1].lower()
        note_val = text[len(parts[0]) + len(parts[1]):].strip()
        chat = get_chat(peer_id)
        chat["notes"][note_name] = note_val
        save_db(db)
        await msg.answer(f"📝 Заметка `{note_name}` сохранена.")
        return

    if cmd == "удалзаметку":
        ok, req = has_cmd_access(peer_id, user_id, "удалзаметку")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        if len(parts) < 2:
            return
        chat = get_chat(peer_id)
        if chat["notes"].pop(parts[1].lower(), None) is not None:
            save_db(db)
            await msg.answer(f"🗑 Заметка `{parts[1].lower()}` удалена.")
        else:
            await msg.answer("❌ Такой заметки нет.")
        return

    if cmd == "заметки":
        chat = get_chat(peer_id)
        if not chat["notes"]:
            await msg.answer("📭 В беседе нет сохранённых заметок.")
            return
        lines = ["📚 **Заметки беседы:**"]
        for k, v in chat["notes"].items():
            lines.append(f"• **{k}**: {v}")
        await msg.answer("\n".join(lines))
        return

    if cmd == "созыв":
        ok, req = has_cmd_access(peer_id, user_id, "созыв")
        if not ok:
            await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
            return
        reason = rest or "Общий сбор!"
        try:
            members = await bot.api.messages.get_conversation_members(peer_id=peer_id)
            mentions = [f"[id{m.member_id}|📣]" for m in members.items if m.member_id > 0]
            await msg.answer(f"📢 **СОЗЫВ БЕСЕДЫ:** {reason}\n" + " ".join(mentions[:60]))
        except Exception as e:
            await msg.answer(f"❌ Не удалось получить список участников (нужна админка боту): `{e}`")
        return


# ==============================================================================
# HTTP HEALTHCHECK ДЛЯ ХОСТИНГА (RENDER / VPS) + ЗАПУСК БОТА
# ==============================================================================
async def health_handler(request: web.Request) -> web.Response:
    return web.Response(text="GAMES | Chat Manager is running!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def main():
    print("[BOOT] Starting GAMES | Chat Manager web server and VK Long Poll...")
    await start_web_server()
    await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
