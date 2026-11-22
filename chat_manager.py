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
    "vk1.a.5BfnzsvcbvRGCC9ZGxmbKdzOU-0NBWIPc9RovnKuIImAHcV46i4wMJUGvrkua3xKhDqo57KVoiXQOqhuctwA93--pk54OWAy_rfzgGfTTQFdG1vFzRObXKPTikSt3h1rRR6ImqGbPMaOgAC3vLaPxx359f9T4N2nl2d4dZGFInNv4F9LLsctf3GiuJfrBkqBcji79CiXrPLqeClPoXaptg"
)
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "897082738").split(",") if x.strip().isdigit()}
DB_FILE = os.getenv("DB_FILE", "games_chat_manager_db.json")
PORT = int(os.getenv("PORT", "10000"))

bot = Bot(token=TOKEN)
ctx = CtxStorage()

# ==============================================================================
# БАЗА ДАННЫХ (АТОМАРНОЕ СОХРАНЕНИЕ)
# ==============================================================================
DEFAULT_DB: Dict[str, Any] = {
    "chats": {},          # peer_id -> settings, ranks, cmd_ranks, warns, admin_warns, mutes, bans, rep, notes, leaders, nicks, marriage, welcome
    "global_admins": {},  # user_id -> rank (0..100)
    "reports": [],        # list of dicts
    "economy": {},        # user_id -> {balance, bank, last_bonus, last_work, businesses, last_collect}
}

# Стандартные минимальные ранги для команд (можно менять отдельно для каждой беседы через /доступ)
DEFAULT_CMD_RANKS: Dict[str, int] = {
    "ник": 0,          # сменить свой ник
    "сник": 15,        # сменить ник другому игроку
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
    "лидер": 60,
    "приветствие": 60,
    "аварн": 75,
    "снятьаварн": 75,
    "ранг": 80,
    "доступ": 95,      # настройка рангов команд в конкретной беседе
}

BUSINESS_CATALOG: Dict[str, Dict[str, Any]] = {
    "1": {"name": "Ларёк с шаурмой", "cost": 5000, "income": 600},
    "2": {"name": "Автомойка", "cost": 25000, "income": 3200},
    "3": {"name": "Клуб / Бар", "cost": 100000, "income": 14000},
    "4": {"name": "IT-Студия", "cost": 350000, "income": 52000},
    "5": {"name": "Нефтевышка", "cost": 1200000, "income": 190000},
}


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


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


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
    if msg.reply_message:
        return msg.reply_message.from_id
    if msg.fwd_messages:
        return msg.fwd_messages[0].from_id
    if arg:
        m = re.search(r"\[id(\d+)\|", arg)
        if m:
            return int(m.group(1))
        m2 = re.search(r"vk\.com/(?:id)?(\d+)", arg)
        if m2:
            return int(m2.group(1))
        if arg.isdigit():
            return int(arg)
    return None


def fmt_num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


# ==============================================================================
# АВТОМОДЕРАЦИЯ И ПРИВЕТСТВИЯ В БЕСЕДАХ
# ==============================================================================
_last_msg_time: Dict[Tuple[int, int], float] = {}
_last_msg_text: Dict[Tuple[int, int], str] = {}


@bot.on.chat_message()
async def chat_guard(msg: Message):
    if not msg.from_id or msg.from_id < 0:
        return
    peer_id = msg.peer_id
    user_id = msg.from_id
    chat = get_chat(peer_id)

    # Системное событие входа нового участника
    if msg.action and msg.action.type.value in ("chat_invite_user", "chat_invite_user_by_link"):
        invited_id = msg.action.member_id or user_id
        if str(invited_id) in chat.get("bans", {}):
            try:
                await bot.api.messages.remove_chat_user(chat_id=peer_id - 2000000000, member_id=invited_id)
                await msg.answer(f"🔨 {await get_user_name(invited_id)} находится в бане этой беседы и был исключён.")
            except Exception:
                pass
            return
        if chat.get("welcome"):
            w_text = chat["welcome"].replace("{user}", await get_user_name(invited_id))
            await msg.answer(w_text)
        return

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

    # Проверка мута
    mute_until = float(chat["mutes"].get(str(user_id), 0))
    if mute_until > now and rank < 30:
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
        except Exception:
            pass
        return
    elif mute_until and mute_until <= now:
        chat["mutes"].pop(str(user_id), None)
        save_db(db)

    # Лок чата
    if chat.get("locked") and rank < 50:
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
        except Exception:
            pass
        return

    text = (msg.text or "").strip()
    if not text or rank >= 30:
        return

    # Антиреклама
    if chat.get("antilink") and re.search(r"(https?://|t\.me/|vk\.me/|vk\.cc/|discord\.gg/)", text, re.I):
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
            await msg.answer(f"🚫 {await get_user_name(user_id)}, ссылки в этом чате запрещены!")
        except Exception:
            pass
        return

    # Антикапс
    letters = [c for c in text if c.isalpha()]
    if chat.get("anticaps") and len(letters) >= 8:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio >= 0.75:
            try:
                await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
                await msg.answer(f"🔠 {await get_user_name(user_id)}, не капси в чате!")
            except Exception:
                pass
            return

    # Антифлуд / Антиспам
    key = (peer_id, user_id)
    if chat.get("antiflood") and now - _last_msg_time.get(key, 0) < 1.2:
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
        except Exception:
            pass
        return
    if chat.get("antispam") and len(text) >= 5 and _last_msg_text.get(key) == text.lower():
        try:
            await bot.api.messages.delete(peer_id=peer_id, cmids=[msg.conversation_message_id], delete_for_all=True)
        except Exception:
            pass
        return

    _last_msg_time[key] = now
    _last_msg_text[key] = text.lower()


# ==============================================================================
# ПОМОЩЬ И ПАНЕЛЬ
# ==============================================================================
HELP_TEXT = (
    "🤖 **GAMES | Чат Менеджер**\n\n"
    "🛡 **Модерация (в беседах):**\n"
    "• `/мут [юзер] [мин] [причина]` | `/размут [юзер]`\n"
    "• `/варн [юзер] [причина]` | `/разварн [юзер]`\n"
    "• `/кик [юзер] [причина]` | `/бан [юзер] [причина]` | `/разбан [id]`\n"
    "• `/чистка [1-100]` | `/закреп` (ответом)\n"
    "• `/чат лок` | `/чат анлок` | `/приветствие [текст|выкл]`\n"
    "• `/защита [антиспам|антикапс|антифлуд|антиреклама] [вкл|выкл]`\n\n"
    "👑 **Админка, права команд и репорты:**\n"
    "• `/ранг [юзер] [0-100]` | `/стата [юзер]` | `/админы`\n"
    "• `/доступ [команда] [ранг]` — настроить нужный ранг команды для ЭТОЙ беседы (от 95+ lvl)\n"
    "• `/доступы` — посмотреть текущие ранги команд в этой беседе\n"
    "• `/аварн [юзер] [причина]` | `/снятьаварн [юзер]`\n"
    "• `/репорт [текст]` | `/репорты` | `/ответ [ID] [текст]`\n\n"
    "📋 **Ники, лидеры, фан и утилиты:**\n"
    "• `/ник [новый_ник]` — поменять свой ник\n"
    "• `/сник [юзер] [новый_ник]` — сменить ник другому (по стандарту с 15 lvl)\n"
    "• `/лидер [фракция] [юзер]` | `/лидеры`\n"
    "• `/заметка [имя] [текст]` | `/заметки` | `/удалзаметку [имя]`\n"
    "• `+реп` / `-реп` (ответом) | `/топ` | `/созыв [текст]`\n"
    "• `/брак [юзер]` | `/развод` | `/шар [вопрос]` | `/выбор [А или Б]`\n\n"
    "🎰 **Игры, казино и бизнесы:**\n"
    "• `/баланс` (`/б`) | `/бонус` | `/работа` | `/перевод [юзер] [сумма]`\n"
    "• `/казино [ставка]` | `/колесо [ставка]` | `/слоты [ставка]` | `/кубик [ставка]`\n"
    "• `/бизнесы` | `/купитьбиз [номер]` | `/мойбиз` | `/собрать`\n\n"
    "⚙️ **Владелец бота:**\n"
    "• `/рассылка [текст]` | `/скачатьбд` | `/выдатьденьги [юзер] [сумма]`"
)


@bot.on.message(text=["/помощь", "помощь", "/help", "/хелп", "/команды", "команды"])
async def cmd_help(msg: Message):
    await msg.answer(HELP_TEXT)


@bot.on.message(text=["/панель", "панель", "/панелл", "панелл", "/menu", "/panel"])
async def cmd_panel(msg: Message):
    kb = (
        Keyboard(inline=True)
        .add(Text("📋 Команды", {"cmd": "help"}), color=KeyboardButtonColor.PRIMARY)
        .add(Text("🎰 Игры и Бизнес", {"cmd": "games"}), color=KeyboardButtonColor.POSITIVE)
        .row()
        .add(Text("📊 Моя стата", {"cmd": "stats"}), color=KeyboardButtonColor.SECONDARY)
        .add(Text("📩 Репорты", {"cmd": "reports"}), color=KeyboardButtonColor.SECONDARY)
    )
    if is_owner(msg.from_id):
        kb.row().add(Text("💾 Скачать БД", {"cmd": "dump_db"}), color=KeyboardButtonColor.NEGATIVE)
    await msg.answer("⚙️ **Панель управления GAMES | Чат Менеджер**\nВыберите нужный раздел:", keyboard=kb)


@bot.on.message(payload={"cmd": "help"})
async def btn_help(msg: Message):
    await msg.answer(HELP_TEXT)


@bot.on.message(payload={"cmd": "games"})
async def btn_games(msg: Message):
    econ = get_econ(msg.from_id)
    await msg.answer(
        f"🎮 **Игровой центр**\n"
        f"💰 Ваш баланс: **{fmt_num(econ['balance'])} $**\n\n"
        f"• `/бонус` — ежедневная награда\n"
        f"• `/работа` — подработка\n"
        f"• `/колесо 500` — Колесо Фортуны\n"
        f"• `/казино 500` — рулетка ставок\n"
        f"• `/слоты 500` — автоматы\n"
        f"• `/кубик 500` — кости с ботом\n"
        f"• `/бизнесы` — покупка и доход с бизнеса"
    )


@bot.on.message(payload={"cmd": "stats"})
async def btn_stats(msg: Message):
    await show_stats(msg, msg.from_id)


@bot.on.message(payload={"cmd": "reports"})
async def btn_reports(msg: Message):
    await cmd_reports(msg)


@bot.on.message(payload={"cmd": "dump_db"})
async def btn_dump(msg: Message):
    await cmd_dump_db(msg)


# ==============================================================================
# НАСТРОЙКА ДОСТУПА КОМАНД ДЛЯ КОНКРЕТНОЙ БЕСЕДЫ (/доступ) — ОТ 95+ LVL
# ==============================================================================
@bot.on.message(text=["/доступ <cmd_name> <lvl_str>", "/доступ"])
async def cmd_set_access(msg: Message, cmd_name: Optional[str] = None, lvl_str: Optional[str] = None):
    if msg.peer_id < 2000000000:
        await msg.answer("❌ Настройка прав команд работает только внутри беседы.")
        return

    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "доступ")
    if not ok:
        await msg.answer(f"⛔ Эта команда доступна с **{req}+ lvl** админки.")
        return

    if not cmd_name or not lvl_str or not lvl_str.isdigit():
        avail = ", ".join(f"`{k}`" for k in sorted(DEFAULT_CMD_RANKS.keys()))
        await msg.answer(
            "⚙️ **Настройка ранга команды для этой беседы:**\n"
            "Формат: `/доступ [команда] [ранг 0-100]`\n"
            "Пример: `/доступ сник 60` или `/доступ сник 16`\n\n"
            f"Доступные команды: {avail}"
        )
        return

    clean_cmd = cmd_name.strip().lower().lstrip("/")
    if clean_cmd not in DEFAULT_CMD_RANKS:
        await msg.answer(f"❌ Команда `{clean_cmd}` не найдена в списке настраиваемых. Напишите `/доступ` для списка.")
        return

    new_lvl = int(lvl_str)
    if not (0 <= new_lvl <= 100):
        await msg.answer("❌ Ранг должен быть от `0` до `100`.")
        return

    chat = get_chat(msg.peer_id)
    chat["cmd_ranks"][clean_cmd] = new_lvl
    save_db(db)
    await msg.answer(
        f"✅ Для **этой беседы** команда `/{clean_cmd}` теперь доступна с **{new_lvl} lvl**!"
    )


@bot.on.message(text=["/доступы", "/права"])
async def cmd_list_access(msg: Message):
    if msg.peer_id < 2000000000:
        await msg.answer("❌ Команда работает только в беседах.")
        return
    chat = get_chat(msg.peer_id)
    custom = chat.get("cmd_ranks", {})
    lines = ["🔐 **Требуемые ранги команд в этой беседе:**"]
    for k, def_v in sorted(DEFAULT_CMD_RANKS.items()):
        cur_v = int(custom.get(k, def_v))
        mark = " *(изменено)*" if k in custom else ""
        lines.append(f"• `/{k}` — **{cur_v} lvl**{mark}")
    await msg.answer("\n".join(lines))


# ==============================================================================
# СИСТЕМА РЕПОРТОВ (ОТВЕТ ИЗ ЛЮБОЙ БЕСЕДЫ -> ПРИХОДИТ В ТУ ЖЕ БЕСЕДУ ИГРОКУ)
# ==============================================================================
@bot.on.message(text=["/репорт <text>", "/report <text>", "!репорт <text>"])
async def cmd_report(msg: Message, text: str):
    text = text.strip()
    if not text:
        await msg.answer("❌ Напишите текст репорта: `/репорт [описание проблемы]`")
        return

    rep_id = len(db["reports"]) + 1
    chat_title = "Личные сообщения"
    if msg.peer_id > 2000000000:
        chat = get_chat(msg.peer_id)
        chat_title = chat.get("title", f"Чат {msg.peer_id}")

    report_obj = {
        "id": rep_id,
        "peer_id": msg.peer_id,
        "chat_title": chat_title,
        "user_id": msg.from_id,
        "text": text,
        "status": "open",
        "answer": None,
        "answered_by": None,
        "ts": int(time.time()),
    }
    db["reports"].append(report_obj)
    save_db(db)

    sender_name = await get_user_name(msg.from_id)
    await msg.answer(
        f"✅ **Репорт #{rep_id} принят!**\n"
        f"Ожидайте ответа администрации прямо в этот чат."
    )

    alert = (
        f"🚨 **Новый репорт #{rep_id}**\n"
        f"👤 От: {sender_name}\n"
        f"💬 Беседа: {chat_title} (`{msg.peer_id}`)\n"
        f"📝 Текст: {text}\n\n"
        f"👉 Ответить из любого чата: `/ответ {rep_id} ваш_текст`"
    )
    for oid in OWNER_IDS:
        try:
            await bot.api.messages.send(peer_id=oid, message=alert, random_id=random.randint(1, 2**31 - 1))
        except Exception:
            pass


@bot.on.message(text=["/репорты", "/reports"])
async def cmd_reports(msg: Message):
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
    lines.append("\nОтветить из любой беседы: `/ответ [ID] [текст ответа]`")
    await msg.answer("\n".join(lines))


@bot.on.message(text=["/ответ <rep_id_str> <answer_text>", "/ответить <rep_id_str> <answer_text>"])
async def cmd_answer_report(msg: Message, rep_id_str: str, answer_text: str):
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "ответ")
    if not ok:
        await msg.answer(f"⛔ Отвечать на репорты можно с **{req}+ lvl**.")
        return
    if not rep_id_str.isdigit():
        await msg.answer("❌ Укажите числовой номер репорта: `/ответ 1 Текст ответа`")
        return

    rep_id = int(rep_id_str)
    target_rep = next((r for r in db["reports"] if r["id"] == rep_id), None)
    if not target_rep:
        await msg.answer(f"❌ Репорт #{rep_id} не найден.")
        return

    admin_name = await get_user_name(msg.from_id)
    user_name = await get_user_name(target_rep["user_id"])
    origin_peer_id = int(target_rep["peer_id"])

    target_rep["status"] = "closed"
    target_rep["answer"] = answer_text.strip()
    target_rep["answered_by"] = msg.from_id
    save_db(db)

    delivery_text = (
        f"📨 **Ответ на ваш репорт #{rep_id}**\n"
        f"👤 Игрок: {user_name}\n"
        f"❓ Ваш вопрос: {target_rep['text']}\n"
        f"🛡 Администратор: {admin_name}\n"
        f"💬 Ответ: **{answer_text.strip()}**"
    )

    try:
        await bot.api.messages.send(
            peer_id=origin_peer_id,
            message=delivery_text,
            random_id=random.randint(1, 2**31 - 1),
        )
        await msg.answer(
            f"✅ Ответ на репорт **#{rep_id}** отправлен игроку в ту беседу (`{target_rep.get('chat_title', origin_peer_id)}`), откуда он писал!"
        )
    except Exception as e:
        await msg.answer(f"⚠️ Статус репорта #{rep_id} обновлён, но не удалось отправить в исходный чат: `{e}`")


# ==============================================================================
# ЭКОНОМИКА, ИГРЫ, КАЗИНО, КОЛЕСО ФОРТУНЫ И БИЗНЕСЫ
# ==============================================================================
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


@bot.on.message(text=["/баланс", "/б", "баланс"])
async def cmd_balance(msg: Message):
    econ = get_econ(msg.from_id)
    pending = collect_passive_income(econ)
    await msg.answer(
        f"💳 **Финансы {await get_user_name(msg.from_id)}**\n"
        f"💵 На руках: **{fmt_num(econ['balance'])} $**\n"
        f"🏢 Накоплено в бизнесах: **{fmt_num(pending)} $** (`/собрать`)"
    )


@bot.on.message(text=["/бонус", "бонус"])
async def cmd_bonus(msg: Message):
    econ = get_econ(msg.from_id)
    now = int(time.time())
    cd = 12 * 3600
    diff = now - int(econ.get("last_bonus", 0))
    if diff < cd:
        rem = cd - diff
        hrs = rem // 3600
        mins = (rem % 3600) // 60
        await msg.answer(f"⏳ Бонус уже получен! Следующий через **{hrs} ч. {mins} мин.**")
        return
    prize = random.randint(1500, 5000)
    econ["balance"] += prize
    econ["last_bonus"] = now
    save_db(db)
    await msg.answer(f"🎁 Вы забрали ежедневный бонус: **+{fmt_num(prize)} $**!\n💵 Баланс: **{fmt_num(econ['balance'])} $**")


@bot.on.message(text=["/работа", "работа"])
async def cmd_work(msg: Message):
    econ = get_econ(msg.from_id)
    now = int(time.time())
    cd = 15 * 60
    diff = now - int(econ.get("last_work", 0))
    if diff < cd:
        mins = (cd - diff) // 60 + 1
        await msg.answer(f"⏳ Вы устали! Отдохните ещё **{mins} мин.**")
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


@bot.on.message(text=["/перевод <target> <amount_str>"])
async def cmd_transfer(msg: Message, target: str, amount_str: str):
    tid = parse_target_id(msg, target)
    if not tid or tid == msg.from_id or not amount_str.isdigit():
        await msg.answer("❌ Использование: `/перевод @пользователь сумма`")
        return
    amount = int(amount_str)
    if amount <= 0:
        await msg.answer("❌ Сумма должна быть больше 0.")
        return
    sender = get_econ(msg.from_id)
    if sender["balance"] < amount:
        await msg.answer("❌ Недостаточно средств на балансе.")
        return
    receiver = get_econ(tid)
    sender["balance"] -= amount
    receiver["balance"] += amount
    save_db(db)
    await msg.answer(f"💸 Перевод **{fmt_num(amount)} $** игроку {await get_user_name(tid)} выполнен успешно!")


def parse_bet(econ: Dict[str, Any], bet_str: str) -> Optional[int]:
    s = bet_str.strip().lower()
    if s in ("все", "всё", "all", "вабанк"):
        return econ["balance"] if econ["balance"] > 0 else None
    if s.isdigit():
        val = int(s)
        return val if val > 0 else None
    return None


@bot.on.message(text=["/казино <bet_str>", "/каз <bet_str>", "казино <bet_str>"])
async def cmd_casino(msg: Message, bet_str: str):
    econ = get_econ(msg.from_id)
    bet = parse_bet(econ, bet_str)
    if not bet:
        await msg.answer("❌ Укажите корректную ставку: `/казино 500` или `/казино все`")
        return
    if econ["balance"] < bet:
        await msg.answer(f"❌ Недостаточно средств! Ваш баланс: **{fmt_num(econ['balance'])} $**")
        return

    roll = random.randint(1, 100)
    if roll <= 46:
        econ["balance"] -= bet
        res = f"📉 Вы проиграли **{fmt_num(bet)} $**."
    elif roll <= 90:
        win = bet
        econ["balance"] += win
        res = f"🎉 Победа (x2)! Вы выиграли **+{fmt_num(win)} $**!"
    else:
        win = bet * 4
        econ["balance"] += win
        res = f"🔥 ДЖЕКПОТ (x5)! Вы сорвали куш **+{fmt_num(win)} $**!"
    save_db(db)
    await msg.answer(f"🎰 **Казино**\n{res}\n💵 Баланс: **{fmt_num(econ['balance'])} $**")


@bot.on.message(text=["/колесо <bet_str>", "колесо <bet_str>"])
async def cmd_wheel(msg: Message, bet_str: str):
    econ = get_econ(msg.from_id)
    bet = parse_bet(econ, bet_str)
    if not bet:
        await msg.answer("❌ Укажите ставку: `/колесо 500`")
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
    names = [s[0] for s in sectors]
    mults = [s[1] for s in sectors]
    weights = [s[2] for s in sectors]
    idx = random.choices(range(len(sectors)), weights=weights, k=1)[0]

    payout = int(bet * mults[idx])
    econ["balance"] = econ["balance"] - bet + payout
    save_db(db)
    diff = payout - bet
    sign = f"+{fmt_num(diff)}" if diff >= 0 else f"{fmt_num(diff)}"
    await msg.answer(
        f"🎡 **Колесо Фортуны**\n"
        f"Сектор: **{names[idx]}**\n"
        f"Итог: **{sign} $**\n"
        f"💵 Баланс: **{fmt_num(econ['balance'])} $**"
    )


@bot.on.message(text=["/слоты <bet_str>", "слоты <bet_str>"])
async def cmd_slots(msg: Message, bet_str: str):
    econ = get_econ(msg.from_id)
    bet = parse_bet(econ, bet_str)
    if not bet:
        await msg.answer("❌ Укажите ставку: `/слоты 500`")
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


@bot.on.message(text=["/кубик <bet_str>", "кубик <bet_str>"])
async def cmd_dice(msg: Message, bet_str: str):
    econ = get_econ(msg.from_id)
    bet = parse_bet(econ, bet_str)
    if not bet:
        await msg.answer("❌ Укажите ставку: `/кубик 500`")
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
        f"🎲 **Кубики**\n"
        f"Вы выбросили: **{p_roll}** | Бот выбросил: **{b_roll}**\n"
        f"{outcome}\n💵 Баланс: **{fmt_num(econ['balance'])} $**"
    )


@bot.on.message(text=["/бизнесы", "бизнесы", "/бизнес"])
async def cmd_businesses(msg: Message):
    lines = ["🏢 **Каталог бизнесов** (пассивный доход каждый час):"]
    for bid, info in BUSINESS_CATALOG.items():
        lines.append(
            f"**{bid}. {info['name']}** — Цена: **{fmt_num(info['cost'])} $** | Доход: **{fmt_num(info['income'])} $/ч**"
        )
    lines.append("\nКупить или улучшить бизнес: `/купитьбиз [номер]`\nЗабрать прибыль: `/собрать`")
    await msg.answer("\n".join(lines))


@bot.on.message(text=["/купитьбиз <bid>"])
async def cmd_buy_biz(msg: Message, bid: str):
    bid = bid.strip()
    if bid not in BUSINESS_CATALOG:
        await msg.answer("❌ Неверный номер бизнеса. Посмотрите `/бизнесы`.")
        return
    econ = get_econ(msg.from_id)
    pending = collect_passive_income(econ)
    econ["balance"] += pending
    econ["last_collect"] = int(time.time())

    cur_lvl = int(econ["businesses"].get(bid, 0))
    info = BUSINESS_CATALOG[bid]
    price = info["cost"] * (cur_lvl + 1)

    if econ["balance"] < price:
        await msg.answer(
            f"❌ Для покупки/улучшения «{info['name']}» (ур. {cur_lvl + 1}) нужно **{fmt_num(price)} $**.\n"
            f"Ваш баланс: **{fmt_num(econ['balance'])} $**"
        )
        return

    econ["balance"] -= price
    econ["businesses"][bid] = cur_lvl + 1
    save_db(db)
    await msg.answer(
        f"✅ Вы приобрели/улучшили **{info['name']}** до **{cur_lvl + 1} ур.** за **{fmt_num(price)} $**!\n"
        f"📈 Новый доход: **{fmt_num(info['income'] * (cur_lvl + 1))} $/ч**"
    )


@bot.on.message(text=["/мойбиз", "/моибизнесы"])
async def cmd_my_biz(msg: Message):
    econ = get_econ(msg.from_id)
    if not econ["businesses"]:
        await msg.answer("🏢 У вас пока нет бизнесов. Откройте `/бизнесы` и купите первый!")
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
    lines.append(f"💰 Накоплено в кассе: **{fmt_num(pending)} $** (`/собрать`)")
    await msg.answer("\n".join(lines))


@bot.on.message(text=["/собрать", "собрать"])
async def cmd_collect(msg: Message):
    econ = get_econ(msg.from_id)
    earned = collect_passive_income(econ)
    if earned <= 0:
        await msg.answer("📭 В кассе бизнесов пока пусто. Приходите чуть позже!")
        return
    econ["balance"] += earned
    econ["last_collect"] = int(time.time())
    save_db(db)
    await msg.answer(f"💰 Вы собрали прибыль с бизнесов: **+{fmt_num(earned)} $**!\n💵 Баланс: **{fmt_num(econ['balance'])} $**")


# ==============================================================================
# МОДЕРАЦИЯ, РАНГИ, НИКИ (/ник И /сник) И ДОП. КОМАНДЫ БЕСЕД
# ==============================================================================
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


@bot.on.message(text=["/стата", "/профиль", "/стата <target>", "/профиль <target>"])
async def cmd_stats(msg: Message, target: Optional[str] = None):
    tid = parse_target_id(msg, target) or msg.from_id
    await show_stats(msg, tid)


@bot.on.message(text=["/ранг <target> <lvl_str>", "/ранг <lvl_str>"])
async def cmd_set_rank(msg: Message, lvl_str: str, target: Optional[str] = None):
    if msg.peer_id < 2000000000:
        await msg.answer("❌ Команда работает в беседах.")
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "ранг")
    if not ok:
        await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
        return
    tid = parse_target_id(msg, target)
    if not tid or not lvl_str.isdigit():
        await msg.answer("❌ Использование: `/ранг @пользователь 50` (или ответом на сообщение)")
        return
    new_lvl = int(lvl_str)
    if not (0 <= new_lvl <= 100):
        await msg.answer("❌ Ранг должен быть от 0 до 100.")
        return
    caller_rank = get_rank(msg.peer_id, msg.from_id)
    if new_lvl >= caller_rank and not is_owner(msg.from_id):
        await msg.answer("⛔ Нельзя выдать ранг выше или равный своему.")
        return
    chat = get_chat(msg.peer_id)
    chat["ranks"][str(tid)] = new_lvl
    save_db(db)
    await msg.answer(f"✅ Игроку {await get_user_name(tid)} установлен ранг **{new_lvl} lvl**.")


@bot.on.message(text=["/админы"])
async def cmd_admins(msg: Message):
    if msg.peer_id < 2000000000:
        return
    chat = get_chat(msg.peer_id)
    lines = ["👑 **Администрация беседы:**"]
    found = False
    for uid_str, lvl in sorted(chat["ranks"].items(), key=lambda x: int(x[1]), reverse=True):
        if int(lvl) > 0:
            found = True
            lines.append(f"• {await get_user_name(int(uid_str))} — **{lvl} lvl**")
    if not found:
        lines.append("Список локальных админов пуст.")
    await msg.answer("\n".join(lines))


# Свой ник (/ник) и чужой ник (/сник — по умолчанию с 15 lvl, настраивается через /доступ сник [ранг])
@bot.on.message(text=["/ник <nick>"])
async def cmd_self_nick(msg: Message, nick: str):
    if msg.peer_id < 2000000000:
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "ник")
    if not ok:
        await msg.answer(f"⛔ Команда `/ник` доступна с **{req}+ lvl**.")
        return
    clean_nick = nick.strip()[:32]
    chat = get_chat(msg.peer_id)
    chat["nicks"][str(msg.from_id)] = clean_nick
    save_db(db)
    await msg.answer(f"🏷 Ваш новый ник в этой беседе: **{clean_nick}**")


@bot.on.message(text=["/сник <target> <nick>", "/сник <nick>"])
async def cmd_set_other_nick(msg: Message, nick: str, target: Optional[str] = None):
    if msg.peer_id < 2000000000:
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "сник")
    if not ok:
        await msg.answer(f"⛔ Команда `/сник` в этой беседе доступна с **{req}+ lvl**.")
        return
    tid = parse_target_id(msg, target)
    if not tid:
        await msg.answer("❌ Укажите игрока: `/сник @пользователь НовыйНик` (или ответом на сообщение)")
        return
    clean_nick = nick.strip()[:32]
    chat = get_chat(msg.peer_id)
    chat["nicks"][str(tid)] = clean_nick
    save_db(db)
    await msg.answer(f"🏷 Игроку {await get_user_name(tid)} установлен ник: **{clean_nick}**")


@bot.on.message(text=["/аварн <target> <reason>", "/аварн <target>", "/аварн"])
async def cmd_admin_warn(msg: Message, target: Optional[str] = None, reason: str = "Нарушение регламента админов"):
    if msg.peer_id < 2000000000:
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "аварн")
    if not ok:
        await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    chat = get_chat(msg.peer_id)
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


@bot.on.message(text=["/снятьаварн", "/снятьаварн <target>"])
async def cmd_remove_admin_warn(msg: Message, target: Optional[str] = None):
    if msg.peer_id < 2000000000:
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "снятьаварн")
    if not ok:
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    chat = get_chat(msg.peer_id)
    aw = max(0, int(chat["admin_warns"].get(str(tid), 0)) - 1)
    chat["admin_warns"][str(tid)] = aw
    save_db(db)
    await msg.answer(f"✅ Снят 1 админ-выговор у {await get_user_name(tid)} (**{aw}/3**).")


@bot.on.message(text=["/мут <target> <mins_str> <reason>", "/мут <target> <mins_str>", "/мут <mins_str>"])
async def cmd_mute(msg: Message, mins_str: str, target: Optional[str] = None, reason: str = "Нарушение правил"):
    if msg.peer_id < 2000000000:
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "мут")
    if not ok:
        await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
        return
    tid = parse_target_id(msg, target)
    if not tid or not mins_str.isdigit():
        await msg.answer("❌ Формат: `/мут @юзер 15 причина`")
        return
    mins = int(mins_str)
    chat = get_chat(msg.peer_id)
    chat["mutes"][str(tid)] = time.time() + mins * 60
    save_db(db)
    await msg.answer(f"🔇 {await get_user_name(tid)} получил мут на **{mins} мин.**\nПричина: {reason}")


@bot.on.message(text=["/размут", "/размут <target>"])
async def cmd_unmute(msg: Message, target: Optional[str] = None):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "размут")
    if not ok:
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    chat = get_chat(msg.peer_id)
    chat["mutes"].pop(str(tid), None)
    save_db(db)
    await msg.answer(f"🔊 Мут с {await get_user_name(tid)} снят.")


@bot.on.message(text=["/варн <target> <reason>", "/варн <target>", "/варн"])
async def cmd_warn(msg: Message, target: Optional[str] = None, reason: str = "Нарушение правил"):
    if msg.peer_id < 2000000000:
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "варн")
    if not ok:
        await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    chat = get_chat(msg.peer_id)
    w = int(chat["warns"].get(str(tid), 0)) + 1
    if w >= 3:
        chat["warns"][str(tid)] = 0
        save_db(db)
        try:
            await bot.api.messages.remove_chat_user(chat_id=msg.peer_id - 2000000000, member_id=tid)
            await msg.answer(f"⛔ {await get_user_name(tid)} получил **3/3 варна** и был исключён!")
        except Exception:
            await msg.answer(f"⚠️ {await get_user_name(tid)} набрал **3/3 варна**, но у бота нет прав кикнуть его.")
    else:
        chat["warns"][str(tid)] = w
        save_db(db)
        await msg.answer(f"⚠️ {await get_user_name(tid)} выдано предупреждение (**{w}/3**).\nПричина: {reason}")


@bot.on.message(text=["/разварн", "/разварн <target>"])
async def cmd_unwarn(msg: Message, target: Optional[str] = None):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "разварн")
    if not ok:
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    chat = get_chat(msg.peer_id)
    w = max(0, int(chat["warns"].get(str(tid), 0)) - 1)
    chat["warns"][str(tid)] = w
    save_db(db)
    await msg.answer(f"✅ Снят 1 варн у {await get_user_name(tid)}. Текущие варны: **{w}/3**.")


@bot.on.message(text=["/кик <target> <reason>", "/кик <target>", "/кик"])
async def cmd_kick(msg: Message, target: Optional[str] = None, reason: str = "Решение администрации"):
    if msg.peer_id < 2000000000:
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "кик")
    if not ok:
        await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    try:
        await bot.api.messages.remove_chat_user(chat_id=msg.peer_id - 2000000000, member_id=tid)
        await msg.answer(f"👢 {await get_user_name(tid)} исключён из беседы.\nПричина: {reason}")
    except Exception as e:
        await msg.answer(f"❌ Не удалось кикнуть (проверьте права администратора у бота): `{e}`")


@bot.on.message(text=["/бан <target> <reason>", "/бан <target>", "/бан"])
async def cmd_ban(msg: Message, target: Optional[str] = None, reason: str = "Бан в беседе"):
    if msg.peer_id < 2000000000:
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "бан")
    if not ok:
        await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    chat = get_chat(msg.peer_id)
    chat["bans"][str(tid)] = reason
    save_db(db)
    try:
        await bot.api.messages.remove_chat_user(chat_id=msg.peer_id - 2000000000, member_id=tid)
    except Exception:
        pass
    await msg.answer(f"🔨 {await get_user_name(tid)} заблокирован в беседе.\nПричина: {reason}")


@bot.on.message(text=["/разбан <target>"])
async def cmd_unban(msg: Message, target: str):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "разбан")
    if not ok:
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    chat = get_chat(msg.peer_id)
    chat["bans"].pop(str(tid), None)
    save_db(db)
    await msg.answer(f"🕊 Блокировка с {await get_user_name(tid)} снята.")


@bot.on.message(text=["/чистка <count_str>"])
async def cmd_clear(msg: Message, count_str: str):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "чистка")
    if not ok or not count_str.isdigit():
        return
    cnt = min(max(1, int(count_str)), 100)
    cmids = list(range(max(1, msg.conversation_message_id - cnt), msg.conversation_message_id + 1))
    try:
        await bot.api.messages.delete(peer_id=msg.peer_id, cmids=cmids, delete_for_all=True)
        await msg.answer(f"🧹 Очищено последних сообщений: **{cnt}**")
    except Exception as e:
        await msg.answer(f"❌ Не удалось удалить сообщения: `{e}`")


@bot.on.message(text=["/закреп"])
async def cmd_pin(msg: Message):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "закреп")
    if not ok:
        return
    if not msg.reply_message:
        await msg.answer("❌ Ответьте командой `/закреп` на сообщение, которое нужно закрепить.")
        return
    try:
        await bot.api.messages.pin(peer_id=msg.peer_id, conversation_message_id=msg.reply_message.conversation_message_id)
        await msg.answer("📌 Сообщение закреплено!")
    except Exception as e:
        await msg.answer(f"❌ Ошибка закрепления: `{e}`")


@bot.on.message(text=["/чат лок", "/чат анлок"])
async def cmd_chat_lock(msg: Message):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "чатлок")
    if not ok:
        return
    chat = get_chat(msg.peer_id)
    lock = "лок" in msg.text.lower() and "анлок" not in msg.text.lower()
    chat["locked"] = lock
    save_db(db)
    await msg.answer("🔒 Чат закрыт для обычных участников." if lock else "🔓 Чат снова открыт для всех!")


@bot.on.message(text=["/приветствие <text>"])
async def cmd_welcome_set(msg: Message, text: str):
    if msg.peer_id < 2000000000:
        return
    ok, req = has_cmd_access(msg.peer_id, msg.from_id, "приветствие")
    if not ok:
        await msg.answer(f"⛔ Доступно с **{req}+ lvl**.")
        return
    chat = get_chat(msg.peer_id)
    if text.strip().lower() in ("выкл", "off", "удалить"):
        chat["welcome"] = ""
        save_db(db)
        await msg.answer("🔕 Приветствие новых участников отключено.")
    else:
        chat["welcome"] = text.strip()
        save_db(db)
        await msg.answer("✅ Приветствие сохранено! Используйте `{user}` для упоминания новичка.")


@bot.on.message(text=["/защита <module> <state>"])
async def cmd_guard_toggle(msg: Message, module: str, state: str):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "защита")
    if not ok:
        return
    mapping = {
        "антиспам": "antispam",
        "антикапс": "anticaps",
        "антифлуд": "antiflood",
        "антиреклама": "antilink",
    }
    key = mapping.get(module.lower())
    if not key:
        await msg.answer("❌ Модули: `антиспам`, `антикапс`, `антифлуд`, `антиреклама`")
        return
    enabled = state.lower() in ("вкл", "on", "1", "да")
    chat = get_chat(msg.peer_id)
    chat[key] = enabled
    save_db(db)
    await msg.answer(f"🛡 Модуль **{module.lower()}** теперь **{'ВКЛЮЧЕН' if enabled else 'ВЫКЛЮЧЕН'}**.")


@bot.on.message(text=["/лидер <faction> <target>", "/лидер <faction>"])
async def cmd_leader(msg: Message, faction: str, target: Optional[str] = None):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "лидер")
    if not ok:
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    chat = get_chat(msg.peer_id)
    chat["leaders"][faction.strip()] = tid
    save_db(db)
    await msg.answer(f"🎖 Назначен лидер фракции **{faction.strip()}**: {await get_user_name(tid)}")


@bot.on.message(text=["/лидеры"])
async def cmd_leaders(msg: Message):
    if msg.peer_id < 2000000000:
        return
    chat = get_chat(msg.peer_id)
    if not chat["leaders"]:
        await msg.answer("📋 Список лидеров пока пуст.")
        return
    lines = ["🎖 **Действующие лидеры:**"]
    for fac, uid in chat["leaders"].items():
        lines.append(f"• **{fac}** — {await get_user_name(int(uid))}")
    await msg.answer("\n".join(lines))


@bot.on.message(text=["/заметка <name> <content>"])
async def cmd_note_add(msg: Message, name: str, content: str):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "заметка")
    if not ok:
        return
    chat = get_chat(msg.peer_id)
    chat["notes"][name.lower()] = content
    save_db(db)
    await msg.answer(f"📝 Заметка `{name.lower()}` сохранена.")


@bot.on.message(text=["/удалзаметку <name>"])
async def cmd_note_del(msg: Message, name: str):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "удалзаметку")
    if not ok:
        return
    chat = get_chat(msg.peer_id)
    if chat["notes"].pop(name.lower(), None) is not None:
        save_db(db)
        await msg.answer(f"🗑 Заметка `{name.lower()}` удалена.")
    else:
        await msg.answer("❌ Такой заметки нет.")


@bot.on.message(text=["/заметки"])
async def cmd_notes_list(msg: Message):
    if msg.peer_id < 2000000000:
        return
    chat = get_chat(msg.peer_id)
    if not chat["notes"]:
        await msg.answer("📭 В беседе нет сохранённых заметок.")
        return
    lines = ["📚 **Заметки беседы:**"]
    for k, v in chat["notes"].items():
        lines.append(f"• **{k}**: {v}")
    await msg.answer("\n".join(lines))


@bot.on.message(text=["+реп", "-реп"])
async def cmd_rep(msg: Message):
    if msg.peer_id < 2000000000 or not msg.reply_message:
        return
    tid = msg.reply_message.from_id
    if tid == msg.from_id or tid < 0:
        return
    chat = get_chat(msg.peer_id)
    delta = 1 if msg.text.strip() == "+реп" else -1
    cur = int(chat["rep"].get(str(tid), 0)) + delta
    chat["rep"][str(tid)] = cur
    save_db(db)
    await msg.answer(f"{'👍' if delta > 0 else '👎'} Репутация {await get_user_name(tid)} изменена! Текущая: **{cur}**")


@bot.on.message(text=["/топ"])
async def cmd_top(msg: Message):
    if msg.peer_id < 2000000000:
        return
    chat = get_chat(msg.peer_id)
    if not chat["rep"]:
        await msg.answer("🏆 Рейтинг репутации пока пуст.")
        return
    top_list = sorted(chat["rep"].items(), key=lambda x: int(x[1]), reverse=True)[:10]
    lines = ["🏆 **Топ участников по репутации:**"]
    for idx, (uid, val) in enumerate(top_list, 1):
        lines.append(f"{idx}. {await get_user_name(int(uid))} — **{val}**")
    await msg.answer("\n".join(lines))


@bot.on.message(text=["/созыв <reason>", "/созыв"])
async def cmd_mention_all(msg: Message, reason: str = "Общий сбор!"):
    if msg.peer_id < 2000000000:
        return
    ok, _ = has_cmd_access(msg.peer_id, msg.from_id, "созыв")
    if not ok:
        return
    try:
        members = await bot.api.messages.get_conversation_members(peer_id=msg.peer_id)
        mentions = [f"[id{m.member_id}|📣]" for m in members.items if m.member_id > 0]
        await msg.answer(f"📢 **СОЗЫВ БЕСЕДЫ:** {reason}\n" + " ".join(mentions[:60]))
    except Exception as e:
        await msg.answer(f"❌ Не удалось получить список участников (нужна админка боту): `{e}`")


# ==============================================================================
# ФАН-КОМАНДЫ ДЛЯ ИГРОКОВ (БРАКИ, ШАР СУДЬБЫ, ВЫБОР)
# ==============================================================================
@bot.on.message(text=["/брак <target>", "/брак"])
async def cmd_marry(msg: Message, target: Optional[str] = None):
    if msg.peer_id < 2000000000:
        return
    tid = parse_target_id(msg, target)
    if not tid or tid == msg.from_id or tid < 0:
        await msg.answer("❌ Укажите игрока для брака: `/брак @пользователь`")
        return
    chat = get_chat(msg.peer_id)
    marriages = chat.setdefault("marriages", {})
    if str(msg.from_id) in marriages or str(tid) in marriages:
        await msg.answer("💔 Кто-то из вас уже состоит в браке в этой беседе!")
        return
    marriages[str(msg.from_id)] = tid
    marriages[str(tid)] = msg.from_id
    save_db(db)
    await msg.answer(f"💍 Поздравляем! {await get_user_name(msg.from_id)} и {await get_user_name(tid)} теперь в браке!")


@bot.on.message(text=["/развод"])
async def cmd_divorce(msg: Message):
    if msg.peer_id < 2000000000:
        return
    chat = get_chat(msg.peer_id)
    marriages = chat.setdefault("marriages", {})
    spouse = marriages.pop(str(msg.from_id), None)
    if not spouse:
        await msg.answer("❌ Вы не состоите в браке.")
        return
    marriages.pop(str(spouse), None)
    save_db(db)
    await msg.answer(f"💔 Брак между {await get_user_name(msg.from_id)} и {await get_user_name(int(spouse))} расторгнут.")


@bot.on.message(text=["/шар <question>"])
async def cmd_8ball(msg: Message, question: str):
    answers = [
        "Бесспорно, да! ✅",
        "Скорее всего, да 👍",
        "Звёзды говорят — нет ❌",
        "Спроси позже, сейчас туманно 🔮",
        "100% инфа, так и будет 🔥",
        "Даже не надейся 😅",
    ]
    await msg.answer(f"🎱 **Шар судьбы:** {random.choice(answers)}")


@bot.on.message(text=["/выбор <options>"])
async def cmd_choose(msg: Message, options: str):
    parts = [p.strip() for p in re.split(r"\s+или\s+|,", options, flags=re.I) if p.strip()]
    if len(parts) < 2:
        await msg.answer("❌ Напишите минимум 2 варианта через «или»: `/выбор чай или кофе`")
        return
    await msg.answer(f"🎯 Я выбираю: **{random.choice(parts)}**")


# ==============================================================================
# КОМАНДЫ ВЛАДЕЛЬЦА БОТА (РАССЫЛКА ТОЛЬКО ТЕБЕ, ДАМП БД, ВЫДАЧА ВАЛЮТЫ)
# ==============================================================================
@bot.on.message(text=["/рассылка <text>", "!рассылка <text>"])
async def cmd_broadcast(msg: Message, text: str):
    if not is_owner(msg.from_id):
        await msg.answer("⛔ Рассылка доступна **только владельцу бота**!")
        return

    sent_ok = 0
    sent_err = 0
    broadcast_msg = f"📢 **Объявление от администрации GAMES:**\n\n{text.strip()}"

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
        f"✅ **Рассылка завершена!**\n"
        f"📨 Доставлено в бесед: **{sent_ok}**\n"
        f"⚠️ Ошибок отправки: **{sent_err}**"
    )


@bot.on.message(text=["/выдатьденьги <target> <amount_str>"])
async def cmd_give_money(msg: Message, target: str, amount_str: str):
    if not is_owner(msg.from_id):
        return
    tid = parse_target_id(msg, target)
    if not tid or not amount_str.isdigit():
        await msg.answer("❌ Использование: `/выдатьденьги @юзер сумма`")
        return
    amount = int(amount_str)
    econ = get_econ(tid)
    econ["balance"] += amount
    save_db(db)
    await msg.answer(f"💸 Выдано **{fmt_num(amount)} $** игроку {await get_user_name(tid)}.")


@bot.on.message(text=["/скачатьбд", "/бекапбд", "/бэкапбд", "/дампбд"])
async def cmd_dump_db(msg: Message):
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
    await start_web_server()
    await bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())
