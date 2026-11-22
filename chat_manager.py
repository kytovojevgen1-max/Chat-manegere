import asyncio
import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web
from vkbottle import BaseStateGroup, CtxStorage, Keyboard, KeyboardButtonColor, Text
from vkbottle.bot import Bot, Message

# ==============================================================================
# КОНФИГУРАЦИЯ
# ==============================================================================
TOKEN = os.getenv(
    "VK_TOKEN",
    "vk1.a.gZ8p6p0tT8k8S3iX6Q8l6R_nK4n3O2M1L0K9J8H7G6F5D4S3A2P1O0I9U8Y7T6R5E4W3Q2"
)
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "849422585").split(",") if x.strip().isdigit()}
DB_FILE = os.getenv("DB_FILE", "games_chat_manager_db.json")
PORT = int(os.getenv("PORT", "10000"))

bot = Bot(token=TOKEN)
ctx = CtxStorage()

# ==============================================================================
# БАЗА ДАННЫХ (АТОМАРНОЕ СОХРАНЕНИЕ)
# ==============================================================================
DEFAULT_DB: Dict[str, Any] = {
    "chats": {},          # peer_id -> settings, users, warns, mutes, bans, reps, notes, leaders
    "global_admins": {},  # user_id -> rank (0..100)
    "reports": [],        # list of dicts {id, peer_id, user_id, text, status, answer, answered_by, ts}
    "economy": {},        # user_id -> {balance, bank, last_bonus, last_work, businesses: {biz_id: level}, last_collect}
    "vip_users": [],      # list of user_ids
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
            "ranks": {},       # user_id -> rank (0..100)
            "warns": {},       # user_id -> count
            "admin_warns": {}, # user_id -> count
            "mutes": {},       # user_id -> until_ts
            "bans": {},        # user_id -> reason
            "rep": {},         # user_id -> int
            "notes": {},       # name -> text
            "leaders": {},     # faction -> user_id
            "nicks": {},       # user_id -> nick
        }
        save_db(db)
    return db["chats"][pid]


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
# АВТОМОДЕРАЦИЯ В БЕСЕДАХ
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
HELP_TEXT = """🤖 **GAMES | Чат Менеджер**

🛡 **Модерация (в беседах):**
• `/мут [юзер] [мин] [причина]` | `/размут [юзер]`
• `/варн [юзер] [причина]` | `/разварн [юзер]`
• `/кик [юзер] [причина]` | `/бан [юзер] [причина]` | `/разбан [id]`
• `/чистка [1-100]` | `/закреп` (ответом)
• `/чат лок` | `/чат анлок`
• `/защита [антиспам|антикапс|антифлуд|антиреклама] [вкл|выкл]`

👑 **Админка, ранги и репорты:**
• `/ранг [юзер] [0-100]` | `/стата [юзер]` | `/админы`
• `/аварн [юзер] [причина]` | `/снятьаварн [юзер]`
• `/репорт [текст]` — отправить жалобу/вопрос админам
• `/репорты` — список открытых репортов
• `/ответ [ID] [текст]` — ответить из ЛЮБОЙ беседы (ответ прилетит игроку в ту беседу, откуда он писал!)

📋 **Чат-утилиты:**
• `/ник [юзер] [ник]` | `/лидер [фракция] [юзер]` | `/лидеры`
• `/заметка [имя] [текст]` | `/заметки` | `/удалзаметку [имя]`
• `+реп` / `-реп` (ответом на сообщение) | `/топ`
• `/созыв [текст]` — позвать активных участников беседы

🎰 **Игры, казино и бизнесы:**
• `/баланс` (`/б`) | `/бонус` | `/работа` | `/перевод [юзер] [сумма]`
• `/казино [ставка]` (`/каз`) — шанс x2 / x5 / 0
• `/колесо [ставка]` — Колесо Фортуны (множители от 0x до 10x)
• `/кубик [ставка]` — дуэль костей против бота
• `/слоты [ставка]` — игровые автоматы 🍒🍋💎7️⃣
• `/бизнесы` — магазин бизнесов | `/купитьбиз [номер]` | `/мойбиз` | `/собрать`

⚙️ **Владелец бота:**
• `/рассылка [текст]` — рассылка по всем беседам (ТОЛЬКО владелец)
• `/скачатьбд` — дамп и сводка базы данных
• `/выдатьденьги [юзер] [сумма]`"""


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
    await msg.answer("⚙️ **Панель управления GAMES | Чат Менеджер**
Выберите нужный раздел:", keyboard=kb)


@bot.on.message(payload={"cmd": "help"})
async def btn_help(msg: Message):
    await msg.answer(HELP_TEXT)


@bot.on.message(payload={"cmd": "games"})
async def btn_games(msg: Message):
    econ = get_econ(msg.from_id)
    await msg.answer(
        f"🎮 **Игровой центр**
"
        f"💰 Ваш баланс: **{fmt_num(econ['balance'])} $**

"
        f"• `/бонус` — ежедневная награда
"
        f"• `/работа` — подработка
"
        f"• `/колесо 500` — Колесо Фортуны
"
        f"• `/казино 500` — рулетка ставок
"
        f"• `/слоты 500` — автоматы
"
        f"• `/кубик 500` — кости с ботом
"
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
        f"✅ **Репорт #{rep_id} принят!**
"
        f"Ожидайте ответа администрации прямо в этот чат."
    )

    # Уведомляем владельцев в ЛС о новом репорте
    alert = (
        f"🚨 **Новый репорт #{rep_id}**
"
        f"👤 От: {sender_name}
"
        f"💬 Беседа: {chat_title} (`{msg.peer_id}`)
"
        f"📝 Текст: {text}

"
        f"👉 Ответить из любого чата: `/ответ {rep_id} ваш_текст`"
    )
    for oid in OWNER_IDS:
        try:
            await bot.api.messages.send(peer_id=oid, message=alert, random_id=random.randint(1, 2**31 - 1))
        except Exception:
            pass


@bot.on.message(text=["/репорты", "/reports"])
async def cmd_reports(msg: Message):
    if get_rank(msg.peer_id, msg.from_id) < 50:
        await msg.answer("⛔ Доступно администраторам с 50+ рангом.")
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
    lines.append("
Ответить из любой беседы: `/ответ [ID] [текст ответа]`")
    await msg.answer("
".join(lines))


@bot.on.message(text=["/ответ <rep_id_str> <answer_text>", "/ответить <rep_id_str> <answer_text>"])
async def cmd_answer_report(msg: Message, rep_id_str: str, answer_text: str):
    if get_rank(msg.peer_id, msg.from_id) < 50:
        await msg.answer("⛔ Отвечать на репорты могут только администраторы (50+ ранг).")
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
        f"📨 **Ответ на ваш репорт #{rep_id}**
"
        f"👤 Игрок: {user_name}
"
        f"❓ Ваш вопрос: {target_rep['text']}
"
        f"🛡 Администратор: {admin_name}
"
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
    # Максимум накопления за 24 часа
    hours = min(hours, 24.0)
    hourly_total = 0
    for biz_id, lvl in econ.get("businesses", {}).items():
        if biz_id in BUSINESS_CATALOG:
            hourly_total += BUSINESS_CATALOG[biz_id]["income"] * int(lvl)
    earned = int(hourly_total * hours)
    return earned


@bot.on.message(text=["/баланс", "/б", "баланс"])
async def cmd_balance(msg: Message):
    econ = get_econ(msg.from_id)
    pending = collect_passive_income(econ)
    await msg.answer(
        f"💳 **Финансы {await get_user_name(msg.from_id)}**
"
        f"💵 На руках: **{fmt_num(econ['balance'])} $**
"
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
    await msg.answer(f"🎁 Вы забрали ежедневный бонус: **+{fmt_num(prize)} $**!
💵 Баланс: **{fmt_num(econ['balance'])} $**")


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
    await msg.answer(f"🛠 Вы {job_desc} и заработали **+{fmt_num(pay)} $**!
💵 Баланс: **{fmt_num(econ['balance'])} $**")


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
    await msg.answer(f"🎰 **Казино**
{res}
💵 Баланс: **{fmt_num(econ['balance'])} $**")


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
        f"🎡 **Колесо Фортуны**
"
        f"Сектор: **{names[idx]}**
"
        f"Итог: **{sign} $**
"
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
    await msg.answer(f"🎰 **Слоты:** {display}
{status}
💵 Баланс: **{fmt_num(econ['balance'])} $**")


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
        f"🎲 **Кубики**
"
        f"Вы выбросили: **{p_roll}** | Бот выбросил: **{b_roll}**
"
        f"{outcome}
💵 Баланс: **{fmt_num(econ['balance'])} $**"
    )


@bot.on.message(text=["/бизнесы", "бизнесы", "/бизнес"])
async def cmd_businesses(msg: Message):
    lines = ["🏢 **Каталог бизнесов** (пассивный доход каждый час):"]
    for bid, info in BUSINESS_CATALOG.items():
        lines.append(
            f"**{bid}. {info['name']}** — Цена: **{fmt_num(info['cost'])} $** | Доход: **{fmt_num(info['income'])} $/ч**"
        )
    lines.append("
Купить или улучшить бизнес: `/купитьбиз [номер]`
Забрать прибыль: `/собрать`")
    await msg.answer("
".join(lines))


@bot.on.message(text=["/купитьбиз <bid>"])
async def cmd_buy_biz(msg: Message, bid: str):
    bid = bid.strip()
    if bid not in BUSINESS_CATALOG:
        await msg.answer("❌ Неверный номер бизнеса. Посмотрите `/бизнесы`.")
        return
    econ = get_econ(msg.from_id)
    # Сначала начислим накопленное по старому уровню
    pending = collect_passive_income(econ)
    econ["balance"] += pending
    econ["last_collect"] = int(time.time())

    cur_lvl = int(econ["businesses"].get(bid, 0))
    info = BUSINESS_CATALOG[bid]
    price = info["cost"] * (cur_lvl + 1)

    if econ["balance"] < price:
        await msg.answer(
            f"❌ Для покупки/улучшения «{info['name']}» (ур. {cur_lvl + 1}) нужно **{fmt_num(price)} $**.
"
            f"Ваш баланс: **{fmt_num(econ['balance'])} $**"
        )
        return

    econ["balance"] -= price
    econ["businesses"][bid] = cur_lvl + 1
    save_db(db)
    await msg.answer(
        f"✅ Вы приобрели/улучшили **{info['name']}** до **{cur_lvl + 1} ур.** за **{fmt_num(price)} $**!
"
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
    lines.append(f"
📊 Общий доход: **{fmt_num(total_hr)} $/ч**")
    lines.append(f"💰 Накоплено в кассе: **{fmt_num(pending)} $** (`/собрать`)")
    await msg.answer("
".join(lines))


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
    await msg.answer(f"💰 Вы собрали прибыль с бизнесов: **+{fmt_num(earned)} $**!
💵 Баланс: **{fmt_num(econ['balance'])} $**")


# ==============================================================================
# МОДЕРАЦИЯ, РАНГИ, УТИЛИТЫ БЕСЕД
# ==============================================================================
async def show_stats(msg: Message, target_id: int):
    rank = get_rank(msg.peer_id, target_id)
    econ = get_econ(target_id)
    name = await get_user_name(target_id)
    warns = 0
    rep = 0
    nick = "Не установлен"
    if msg.peer_id > 2000000000:
        chat = get_chat(msg.peer_id)
        warns = int(chat["warns"].get(str(target_id), 0))
        rep = int(chat["rep"].get(str(target_id), 0))
        nick = chat["nicks"].get(str(target_id), "Не установлен")
    await msg.answer(
        f"📊 **Профиль:** {name}
"
        f"👑 Ранг: **{rank} lvl**
"
        f"🏷 Ник в чате: **{nick}**
"
        f"⭐ Репутация: **{rep}** | ⚠️ Варны: **{warns}/3**
"
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
    tid = parse_target_id(msg, target)
    if not tid or not lvl_str.isdigit():
        await msg.answer("❌ Использование: `/ранг @пользователь 50` (или ответом на сообщение)")
        return
    new_lvl = int(lvl_str)
    if not (0 <= new_lvl <= 100):
        await msg.answer("❌ Ранг должен быть от 0 до 100.")
        return
    caller_rank = get_rank(msg.peer_id, msg.from_id)
    if caller_rank < 80 or (new_lvl >= caller_rank and not is_owner(msg.from_id)):
        await msg.answer("⛔ Недостаточно прав для выдачи этого ранга.")
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
    await msg.answer("
".join(lines))


@bot.on.message(text=["/мут <target> <mins_str> <reason>", "/мут <target> <mins_str>", "/мут <mins_str>"])
async def cmd_mute(msg: Message, mins_str: str, target: Optional[str] = None, reason: str = "Нарушение правил"):
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 30:
        return
    tid = parse_target_id(msg, target)
    if not tid or not mins_str.isdigit():
        await msg.answer("❌ Формат: `/мут @юзер 15 причина`")
        return
    mins = int(mins_str)
    chat = get_chat(msg.peer_id)
    chat["mutes"][str(tid)] = time.time() + mins * 60
    save_db(db)
    await msg.answer(f"🔇 {await get_user_name(tid)} получил мут на **{mins} мин.**
Причина: {reason}")


@bot.on.message(text=["/размут", "/размут <target>"])
async def cmd_unmute(msg: Message, target: Optional[str] = None):
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 30:
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
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 40:
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
        await msg.answer(f"⚠️ {await get_user_name(tid)} выдано предупреждение (**{w}/3**).
Причина: {reason}")


@bot.on.message(text=["/разварн", "/разварн <target>"])
async def cmd_unwarn(msg: Message, target: Optional[str] = None):
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 40:
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
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 50:
        return
    tid = parse_target_id(msg, target)
    if not tid:
        return
    try:
        await bot.api.messages.remove_chat_user(chat_id=msg.peer_id - 2000000000, member_id=tid)
        await msg.answer(f"👢 {await get_user_name(tid)} исключён из беседы.
Причина: {reason}")
    except Exception as e:
        await msg.answer(f"❌ Не удалось кикнуть (проверьте права администратора у бота): `{e}`")


@bot.on.message(text=["/бан <target> <reason>", "/бан <target>", "/бан"])
async def cmd_ban(msg: Message, target: Optional[str] = None, reason: str = "Бан в беседе"):
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 60:
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
    await msg.answer(f"🔨 {await get_user_name(tid)} заблокирован в беседе.
Причина: {reason}")


@bot.on.message(text=["/разбан <target>"])
async def cmd_unban(msg: Message, target: str):
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 60:
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
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 50:
        return
    if not count_str.isdigit():
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
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 40:
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
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 60:
        return
    chat = get_chat(msg.peer_id)
    lock = "лок" in msg.text.lower() and "анлок" not in msg.text.lower()
    chat["locked"] = lock
    save_db(db)
    await msg.answer("🔒 Чат закрыт для обычных участников." if lock else "🔓 Чат снова открыт для всех!")


@bot.on.message(text=["/защита <module> <state>"])
async def cmd_guard_toggle(msg: Message, module: str, state: str):
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 60:
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


@bot.on.message(text=["/ник <target> <nick>", "/ник <nick>"])
async def cmd_nick(msg: Message, nick: str, target: Optional[str] = None):
    if msg.peer_id < 2000000000:
        return
    tid = parse_target_id(msg, target) or msg.from_id
    if tid != msg.from_id and get_rank(msg.peer_id, msg.from_id) < 40:
        await msg.answer("⛔ Менять ник другим игрокам могут админы с 40+ рангом.")
        return
    chat = get_chat(msg.peer_id)
    chat["nicks"][str(tid)] = nick.strip()[:32]
    save_db(db)
    await msg.answer(f"🏷 Игроку {await get_user_name(tid)} установлен ник: **{nick.strip()[:32]}**")


@bot.on.message(text=["/лидер <faction> <target>", "/лидер <faction>"])
async def cmd_leader(msg: Message, faction: str, target: Optional[str] = None):
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 60:
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
    await msg.answer("
".join(lines))


@bot.on.message(text=["/заметка <name> <content>"])
async def cmd_note_add(msg: Message, name: str, content: str):
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 40:
        return
    chat = get_chat(msg.peer_id)
    chat["notes"][name.lower()] = content
    save_db(db)
    await msg.answer(f"📝 Заметка `{name.lower()}` сохранена.")


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
    await msg.answer("
".join(lines))


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
    await msg.answer("
".join(lines))


@bot.on.message(text=["/созыв <reason>", "/созыв"])
async def cmd_mention_all(msg: Message, reason: str = "Общий сбор!"):
    if msg.peer_id < 2000000000 or get_rank(msg.peer_id, msg.from_id) < 50:
        return
    try:
        members = await bot.api.messages.get_conversation_members(peer_id=msg.peer_id)
        mentions = []
        for m in members.items:
            if m.member_id > 0:
                mentions.append(f"[id{m.member_id}|📣]")
        await msg.answer(f"📢 **СОЗЫВ БЕСЕДЫ:** {reason}
" + " ".join(mentions[:60]))
    except Exception as e:
        await msg.answer(f"❌ Не удалось получить список участников (нужна админка боту): `{e}`")


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
    broadcast_msg = f"📢 **Объявление от администрации GAMES:**

{text.strip()}"

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
        f"✅ **Рассылка завершена!**
"
        f"📨 Доставлено в бесед: **{sent_ok}**
"
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
        f"💾 **Сводка базы данных GAMES | Чат Менеджер**
"
        f"• Активных бесед: **{len(db['chats'])}**
"
        f"• Игроков в экономике: **{len(db['economy'])}**
"
        f"• Всего репортов: **{len(db['reports'])}**"
    )
    raw = json.dumps(db, ensure_ascii=False, indent=2)
    if len(raw) <= 3500:
        await msg.answer(f"{summary}

```json
{raw}
```")
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
