import asyncio
import html
import logging
import os
import random
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    User,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("navbatchi")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
SUPER_ADMIN_ID = os.getenv("SUPER_ADMIN_ID")  # ixtiyoriy: admin Telegram ID si
ADMIN_CODE = os.getenv("ADMIN_CODE")  # ixtiyoriy: /admin KOD orqali admin bo'lish
PORT = int(os.getenv("PORT", "10000"))
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Tashkent"))
SELF_URL = os.getenv("RENDER_EXTERNAL_URL")

# Kechki smena 19:00 da boshlanadi va ertasi kuni 02:30 gacha davom etadi.
REM_SLOTS = {(h, m) for h in (19, 20, 21, 22, 23, 0, 1) for m in (0, 30)} | {(2, 0), (2, 30)}
GROUP_SLOTS = {(19, 0), (21, 0), (23, 0), (1, 0), (2, 30)}
FINAL_SLOT = (2, 31)
EVE_START = (19, 0)
DEFAULT_DAY_TIMES = "09:00,13:00,17:00"
MAX_PHOTOS = 10
CARRY_OVER_MISSED = True  # bajarilmagan (missed) navbatchilik ertasi kuni o'sha odamda qoladi
GROUP_TYPES = ("group", "supergroup")

pool: asyncpg.Pool
router = Router()
background_tasks: list[asyncio.Task] = []

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    full_name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    excluded BOOLEAN NOT NULL DEFAULT FALSE,
    seq BIGSERIAL
);
ALTER TABLE members ADD COLUMN IF NOT EXISTS pos BIGINT;
CREATE TABLE IF NOT EXISTS shifts (
    day DATE PRIMARY KEY,
    user_id BIGINT NOT NULL,
    username TEXT,
    full_name TEXT NOT NULL,
    seq BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    admin_comment TEXT
);
CREATE TABLE IF NOT EXISTS shift_photos (
    id BIGSERIAL PRIMARY KEY,
    day DATE NOT NULL,
    file_id TEXT NOT NULL
);
"""

STATUS_TEXT = {
    "open": "⏳ kutilmoqda",
    "collecting": "📸 rasm yuklanmoqda",
    "review": "🕵️ admin tekshirmoqda",
    "approved": "✅ tasdiqlangan",
    "missed": "❌ bajarilmagan",
}

DAY_TEXTS = [
    "⏰ {who}, bugun navbatchilik sizda. Belgilangan vaqtda navbatchilikni bajaring!",
    "📌 {who}, navbatchilikni unutmang. Kun tugamasdan bajarib, rasmini yuboring.",
    "🧹 Hurmatli {who}, bugungi navbatchilik sizniki. Bajarib bo'lgach «Bajardim» tugmasini bosing.",
    "🔔 {who}, eslatma: bugun navbatchi siz. Vaqtida bajarsangiz, hammaga yengil bo'ladi 🙂",
]
EVE_TEXTS = [
    "⏳ {who}, navbatchilikni bajarish vaqti keldi!",
    "🔔 {who}, navbatchilik hali bajarilmagan. Iltimos, hoziroq bajaring.",
    "📢 {who}, navbatchilikni bajar va rasmini yubor!",
    "🧹 {who}, bugungi navbatchilik sizda. Bajarib bo'lgach «Bajardim» tugmasini bosing.",
]

LINE = "━━━━━━━━━━━━━━━━"


# ------------------------------------------------------------------ helpers
def shift_day(now: datetime) -> date:
    """02:31 gacha bo'lgan vaqt kechagi smenaga tegishli."""
    if (now.hour, now.minute) <= FINAL_SLOT:
        return now.date() - timedelta(days=1)
    return now.date()


def who(r) -> str:
    s = html.escape(r["full_name"])
    return f"{s} (@{r['username']})" if r["username"] else s


def who_group(r) -> str:
    if r["username"]:
        return f"{html.escape(r['full_name'])} (@{r['username']})"
    return f'<a href="tg://user?id={r["user_id"]}">{html.escape(r["full_name"])}</a>'


def plain(r) -> str:
    return f"@{r['username']}" if r["username"] else r["full_name"]


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def bajardim_kb(day: date):
    return kb([("✅ Bajardim", f"bajardim:{day.isoformat()}")])


def send_kb(day: date):
    return kb([("📤 Adminga yuborish", f"send:{day.isoformat()}")])


def review_kb(day: date):
    d = day.isoformat()
    return kb([("✅ Bajarildi", f"rv:ok:{d}"), ("❌ Bajarilmadi", f"rv:no:{d}")])


def user_menu_kb():
    return kb(
        [("📅 Bugungi navbatchi", "menu:today"), ("👥 Navbat tartibi", "menu:members")],
        [("✅ Bajardim", "menu:bajardim"), ("✏️ Ismni o'zgartirish", "menu:name")],
    )


def admin_menu_kb():
    return kb(
        [("📅 Bugungi navbatchi", "menu:today"), ("👥 Navbat tartibi", "menu:members")],
        [("✅ Bajardim", "menu:bajardim"), ("✏️ Ismni o'zgartirish", "menu:name")],
        [("👑 Admin paneli", "adm:panel")],
    )


async def safe_send(bot: Bot, chat_id: int, text: str, **kw):
    try:
        return await bot.send_message(chat_id, text, **kw)
    except Exception as e:
        log.warning("yuborib bo'lmadi (%s): %s", chat_id, e)
        return None


async def show(cb: CallbackQuery, text: str, markup=None):
    """Xabarni tahrirlaydi, bo'lmasa yangisini yuboradi."""
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)


async def get_setting(key: str, default=None):
    row = await pool.fetchrow("SELECT value FROM settings WHERE key=$1", key)
    return row["value"] if row else default


async def set_setting(key: str, value: str):
    await pool.execute(
        "INSERT INTO settings(key,value) VALUES($1,$2) "
        "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
        key,
        value,
    )


async def get_admin_id() -> int | None:
    v = await get_setting("admin_id")
    return int(v) if v else None


async def is_admin(uid: int) -> bool:
    return uid == await get_admin_id()


async def get_chat_id() -> int | None:
    v = await get_setting("chat_id")
    return int(v) if v else None


async def is_started() -> bool:
    return (await get_setting("started")) == "1"


def valid_day_time(h: int, m: int) -> bool:
    return FINAL_SLOT < (h, m) < EVE_START


async def get_day_times() -> list[tuple[int, int]]:
    raw = await get_setting("day_times", DEFAULT_DAY_TIMES)
    out = []
    for part in raw.split(","):
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", part.strip())
        if m and valid_day_time(int(m[1]), int(m[2])):
            out.append((int(m[1]), int(m[2])))
    return sorted(set(out))


# ------------------------------------------------------------------ members
async def register_member(u: User, name: str | None = None):
    await pool.execute(
        """
        INSERT INTO members(user_id, username, full_name, pos)
        VALUES($1,$2,$3,(SELECT COALESCE(MAX(pos),0)+1 FROM members))
        ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username
        """,
        u.id,
        (u.username or "").lower() or None,
        name or u.full_name,
    )


async def set_name(u: User, name: str):
    await register_member(u, name)
    await pool.execute("UPDATE members SET full_name=$2 WHERE user_id=$1", u.id, name)


async def active_members():
    return await pool.fetch("SELECT * FROM members WHERE active AND NOT excluded ORDER BY pos, seq")


async def all_members():
    return await pool.fetch("SELECT * FROM members ORDER BY pos, seq")


def next_after(members, pos):
    if not members:
        return None
    if pos is None:
        return members[0]
    for m in members:
        if m["pos"] > pos:
            return m
    return members[0]


async def get_or_create_shift(day: date):
    row = await pool.fetchrow("SELECT * FROM shifts WHERE day=$1", day)
    if row:
        return row
    if not await is_started():
        return None
    members = await active_members()
    if not members:
        return None
    sd = await get_setting("start_day")
    if sd:
        prev = await pool.fetchrow(
            "SELECT seq, status FROM shifts WHERE day<$1 AND day>=$2 ORDER BY day DESC LIMIT 1",
            day, date.fromisoformat(sd),
        )
    else:
        prev = await pool.fetchrow("SELECT seq, status FROM shifts WHERE day<$1 ORDER BY day DESC LIMIT 1", day)
    pick = None
    if prev and CARRY_OVER_MISSED and prev["status"] == "missed":
        # bajarmagan odam yana o'sha navbatchilikni qiladi
        pick = next((m for m in members if m["pos"] == prev["seq"]), None)
    if pick is None:
        pick = next_after(members, prev["seq"] if prev else None)
    await pool.execute(
        "INSERT INTO shifts(day,user_id,username,full_name,seq) VALUES($1,$2,$3,$4,$5) "
        "ON CONFLICT DO NOTHING",
        day, pick["user_id"], pick["username"], pick["full_name"], pick["pos"],
    )
    return await pool.fetchrow("SELECT * FROM shifts WHERE day=$1", day)


async def photo_ids(day: date) -> list[str]:
    rows = await pool.fetch("SELECT file_id FROM shift_photos WHERE day=$1 ORDER BY id", day)
    return [r["file_id"] for r in rows]


# ---------------------------------------------------------- group binding
async def bind_chat(bot: Bot, chat_id: int):
    await set_setting("chat_id", str(chat_id))
    me = await bot.get_me()
    await safe_send(
        bot,
        chat_id,
        "👋 <b>Navbatchilik boti ulandi!</b>\n\n"
        "Navbatchilar ro'yxatiga kirish uchun pastdagi tugma orqali botga o'ting va "
        "<b>Start</b> bosing. Har bir a'zo buni bir marta qilishi kerak.\n\n"
        "🕖 Kechki eslatmalar har kuni <b>19:00 dan 02:30 gacha</b> yuboriladi.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="➡️ Botga o'tish", url=f"https://t.me/{me.username}?start=reg")]]
        ),
    )


@router.my_chat_member()
async def on_my_status(ev: ChatMemberUpdated):
    if ev.chat.type not in GROUP_TYPES:
        return
    if ev.new_chat_member.status not in ("member", "administrator"):
        return
    if ev.old_chat_member.status in ("member", "administrator"):
        return
    if not await is_admin(ev.from_user.id):
        log.info("Admin bo'lmagan foydalanuvchi botni guruhga qo'shdi, e'tiborsiz: %s", ev.chat.id)
        return
    await bind_chat(ev.bot, ev.chat.id)


@router.message(Command("ulash"))
async def cmd_bind(message: Message):
    if message.chat.type not in GROUP_TYPES:
        return await message.answer("Bu buyruqni guruhda yozing.")
    if not await is_admin(message.from_user.id):
        return await message.reply("Faqat super admin uchun.")
    await bind_chat(message.bot, message.chat.id)


# ------------------------------------------------- registratsiya (shaxsiy chat)
PRIVATE = F.chat.type == "private"


async def menu_for(uid: int):
    return admin_menu_kb() if await is_admin(uid) else user_menu_kb()


@router.message(Command("start", "yordam"), PRIVATE)
async def cmd_start(message: Message):
    u = message.from_user
    exists = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", u.id)
    if exists:
        await register_member(u)
        exists = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", u.id)
        text = f"👋 Salom, <b>{who(exists)}</b>!\nSiz navbatchilar ro'yxatidasiz ✅"
    elif u.username:
        await register_member(u)
        row = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", u.id)
        text = f"🎉 Ro'yxatdan o'tdingiz: <b>{who(row)}</b>"
        await notify_admin_new(message.bot, row)
    else:
        await set_setting(f"await:{u.id}", "1")
        return await message.answer(
            "Salom! Sizda Telegram username yo'q ekan.\n✏️ Iltimos, ism-familiyangizni yozib yuboring."
        )

    if not await is_started():
        text += "\n\n⏳ Navbatchilik hali boshlanmagan. Admin boshlaganda tartib e'lon qilinadi."
    text += f"\n{LINE}\nPastdagi tugmalardan foydalaning 👇"
    await message.answer(text, reply_markup=await menu_for(u.id))


async def notify_admin_new(bot: Bot, row):
    admin = await get_admin_id()
    if admin and admin != row["user_id"]:
        total = await pool.fetchval("SELECT COUNT(*) FROM members WHERE active AND NOT excluded")
        await safe_send(bot, admin, f"🆕 Yangi a'zo: <b>{who(row)}</b>\n👥 Jami: {total} ta",
                        reply_markup=kb([("👑 Admin paneli", "adm:panel")]))


@router.message(Command("ism"), PRIVATE)
async def cmd_name(message: Message):
    await set_setting(f"await:{message.from_user.id}", "1")
    await message.answer("✏️ Yangi ism-familiyangizni yozib yuboring.")


@router.callback_query(F.data == "menu:name")
async def menu_name(cb: CallbackQuery):
    await set_setting(f"await:{cb.from_user.id}", "1")
    await cb.answer()
    await cb.message.answer("✏️ Yangi ism-familiyangizni yozib yuboring.")


@router.message(Command("admin"), PRIVATE)
async def cmd_admin(message: Message, command: CommandObject):
    if SUPER_ADMIN_ID:
        return await message.answer("Admin SUPER_ADMIN_ID orqali belgilangan, kod bilan o'zgartirib bo'lmaydi.")
    if ADMIN_CODE and (command.args or "").strip() == ADMIN_CODE:
        await set_setting("admin_id", str(message.from_user.id))
        return await message.answer("👑 Siz endi super adminsiz.", reply_markup=admin_menu_kb())
    await message.answer("Kod noto'g'ri yoki ADMIN_CODE sozlanmagan.")


@router.message(F.text, PRIVATE, ~F.text.startswith("/"))
async def on_text(message: Message):
    u = message.from_user
    uid = u.id
    member = await pool.fetchrow("SELECT user_id FROM members WHERE user_id=$1", uid)

    if await get_setting(f"await:{uid}") or (not member and not u.username):
        name = message.text.strip()[:64]
        if len(name) < 2:
            return await message.answer("Ism juda qisqa, qaytadan yozing.")
        await set_setting(f"await:{uid}", "")
        await set_name(u, name)
        row = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", uid)
        await message.answer(f"✅ Saqlandi: <b>{who(row)}</b>", reply_markup=await menu_for(uid))
        if not member:
            await notify_admin_new(message.bot, row)
        return

    if await is_admin(uid) and await get_setting("admin_pending"):
        return await finalize_review(message.bot, message.text.strip())

    if not member:
        await message.answer("Ro'yxatdan o'tish uchun /start bosing.")


# --------------------------------------------------------------- umumiy ko'rinishlar
async def today_text() -> str:
    if not await is_started():
        n = await pool.fetchval("SELECT COUNT(*) FROM members WHERE active AND NOT excluded")
        return (f"⏳ Navbatchilik hali boshlanmagan.\n👥 Ro'yxatda: {n} kishi.\n"
                "Admin «Boshlash» tugmasini bosganda tartib shakllanadi.")
    row = await get_or_create_shift(shift_day(datetime.now(TZ)))
    if not row:
        return "Ro'yxat bo'sh. Botga /start bosib ro'yxatdan o'ting."
    return f"📅 <b>Bugungi navbatchi</b>\n{LINE}\n👤 <b>{who(row)}</b>\n{STATUS_TEXT[row['status']]}"


async def members_text() -> str:
    rows = await all_members()
    if not rows:
        return "Ro'yxat hali bo'sh."
    started = await is_started()
    cur_id = None
    if started:
        cur = await pool.fetchrow("SELECT user_id FROM shifts WHERE day=$1", shift_day(datetime.now(TZ)))
        cur_id = cur["user_id"] if cur else None
    lines = []
    for i, m in enumerate(rows, 1):
        mark = "⛔" if (m["excluded"] or not m["active"]) else ("👉" if m["user_id"] == cur_id else "▫️")
        lines.append(f"{mark} {i}. {who(m)}")
    head = "👥 <b>Navbat tartibi</b>" if started else "👥 <b>Ro'yxatdan o'tganlar</b> (tartib hali shakllanmagan)"
    return f"{head}\n{LINE}\n" + "\n".join(lines)


@router.message(Command("bugun"))
async def cmd_today(message: Message):
    await message.reply(await today_text())


@router.message(Command("azolar"))
async def cmd_members(message: Message):
    await message.reply(await members_text())


@router.callback_query(F.data == "menu:today")
async def menu_today(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(await today_text())


@router.callback_query(F.data == "menu:members")
async def menu_members(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(await members_text())


@router.callback_query(F.data == "menu:bajardim")
async def menu_bajardim(cb: CallbackQuery):
    row = await pool.fetchrow(
        "SELECT * FROM shifts WHERE user_id=$1 AND status IN ('open','collecting') ORDER BY day DESC LIMIT 1",
        cb.from_user.id,
    )
    if not row:
        return await cb.answer("Hozir sizda ochiq navbatchilik yo'q.", show_alert=True)
    await cb.answer()
    await start_collecting(cb.bot, row)


# -------------------------------------------------------- admin paneli
async def admin_only(message: Message) -> bool:
    if not await is_admin(message.from_user.id):
        await message.reply("Faqat super admin uchun.")
        return False
    return True


async def panel_text() -> str:
    started = await is_started()
    n = await pool.fetchval("SELECT COUNT(*) FROM members WHERE active AND NOT excluded")
    times = ", ".join(f"{h:02d}:{m:02d}" for h, m in await get_day_times()) or "yo'q"
    chat = "ulangan ✅" if await get_chat_id() else "ulanmagan ⚠️ (guruhda /ulash)"
    if started:
        sd = await get_setting("start_day")
        cur = await get_or_create_shift(shift_day(datetime.now(TZ)))
        st = f"🟢 Boshlangan{f' ({date.fromisoformat(sd).strftime(chr(37)+chr(100)+chr(46)+chr(37)+chr(109)+chr(46)+chr(37)+chr(89))})' if sd else ''}"
        now_line = f"\n📅 Bugun: <b>{who(cur)}</b> — {STATUS_TEXT[cur['status']]}" if cur else ""
    else:
        st = "🔴 Hali boshlanmagan"
        now_line = ""
    return (
        f"👑 <b>Admin paneli</b>\n{LINE}\n"
        f"Holat: {st}\n👥 Faol a'zolar: <b>{n}</b>{now_line}\n"
        f"⏰ Kunduzgi vaqtlar: {times}\n💬 Guruh: {chat}"
    )


def panel_kb(started: bool):
    rows = []
    if started:
        rows.append([("🔀 Tartibni o'zgartirish", "adm:order")])
        rows.append([("🔁 Navbatchini o'tkazish", "adm:skip")])
        rows.append([("👤 Bugungi navbatchini almashtirish", "adm:assign")])
    else:
        rows.append([("🚀 Navbatchilikni boshlash", "adm:order")])
    rows.append([("👥 A'zolar", "menu:members"), ("🔄 Yangilash", "adm:panel")])
    return kb(*rows)


@router.message(Command("panel"), PRIVATE)
async def cmd_panel(message: Message):
    if not await admin_only(message):
        return
    await message.answer(await panel_text(), reply_markup=panel_kb(await is_started()))


@router.callback_query(F.data == "adm:panel")
async def cb_panel(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Faqat super admin uchun.", show_alert=True)
    await cb.answer()
    await show(cb, await panel_text(), panel_kb(await is_started()))


# ---- tartib tuzish: tasodifiy yoki qo'lda
async def draft_get():
    mode = await get_setting("draft_mode", "")
    raw = await get_setting("draft", "")
    ids = [int(x) for x in raw.split(",") if x]
    return mode, ids


async def draft_set(mode: str, ids: list[int]):
    await set_setting("draft_mode", mode)
    await set_setting("draft", ",".join(map(str, ids)))


async def draft_view():
    mode, ids = await draft_get()
    members = await active_members()
    mem = {m["user_id"]: m for m in members}
    ids = [i for i in ids if i in mem]
    rest = [m for m in members if m["user_id"] not in ids]
    started = await is_started()
    ok_label = "✅ Saqlash" if started else "🚀 Tasdiqlash va boshlash"
    lines = [f"{i}. {who(mem[u])}" for i, u in enumerate(ids, 1)]

    if mode == "rand":
        text = (f"🎲 <b>Tasodifiy tartib</b>\n{LINE}\n" + "\n".join(lines) +
                "\n\nTartib ma'qulmi? Birinchi bo'lib 1-o'rindagi odam navbatchi bo'ladi.")
        markup = kb(
            [(ok_label, "st:ok")],
            [("🎲 Qayta aralashtirish", "st:rand"), ("✋ Qo'lda tanlash", "st:man")],
            [("❌ Bekor qilish", "st:cancel")],
        )
        return text, markup

    # qo'lda tanlash
    text = f"✋ <b>Qo'lda tartib tuzish</b>\n{LINE}\n"
    text += ("Navbatga qo'shiladigan odamni tanlang (tartib bo'yicha 1, 2, 3...):\n\n" if rest
             else "Hamma tanlandi 👇\n\n")
    if lines:
        text += "<b>Hozirgi tartib:</b>\n" + "\n".join(lines)
    else:
        text += "<i>Hali hech kim tanlanmadi.</i>"
    rows = []
    row = []
    for m in rest:
        label = (m["full_name"] if len(m["full_name"]) <= 18 else m["full_name"][:17] + "…")
        row.append((f"➕ {label}", f"pk:{m['user_id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    ctrl = []
    if ids:
        ctrl.append(("↩️ Orqaga", "st:undo"))
    if rest and ids:
        ctrl.append(("🎲 Qolganini aralashtir", "st:fill"))
    if ctrl:
        rows.append(ctrl)
    if not rest and ids:
        rows.append([(ok_label, "st:ok")])
    rows.append([("🎲 Hammasini tasodifiy", "st:rand"), ("❌ Bekor", "st:cancel")])
    return text, kb(*rows)


async def draft_show(cb: CallbackQuery):
    text, markup = await draft_view()
    await show(cb, text, markup)


@router.callback_query(F.data.in_({"adm:order", "st:rand", "st:man", "st:undo", "st:fill", "st:cancel", "st:ok"}) |
                       F.data.startswith("pk:"))
async def cb_order(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Faqat super admin uchun.", show_alert=True)
    data = cb.data
    members = await active_members()

    if data == "adm:order":
        if len(members) < 1:
            return await cb.answer("Ro'yxat bo'sh. Avval a'zolar /start bosishi kerak.", show_alert=True)
        started = await is_started()
        title = "🔀 <b>Tartibni o'zgartirish</b>" if started else "🚀 <b>Navbatchilikni boshlash</b>"
        await cb.answer()
        return await show(
            cb,
            f"{title}\n{LINE}\n👥 Ro'yxatda: <b>{len(members)}</b> kishi\n\n"
            "Navbat tartibini qanday tuzamiz?",
            kb([("🎲 Tasodifiy", "st:rand")], [("✋ Qo'lda tanlayman", "st:man")],
               [("⬅️ Orqaga", "adm:panel")]),
        )

    if data == "st:cancel":
        await draft_set("", [])
        await cb.answer("Bekor qilindi")
        return await show(cb, await panel_text(), panel_kb(await is_started()))

    if data == "st:rand":
        ids = [m["user_id"] for m in members]
        random.shuffle(ids)
        await draft_set("rand", ids)
        await cb.answer("🎲 Aralashtirildi")
        return await draft_show(cb)

    if data == "st:man":
        await draft_set("man", [])
        await cb.answer()
        return await draft_show(cb)

    mode, ids = await draft_get()

    if data.startswith("pk:"):
        uid = int(data.split(":", 1)[1])
        if uid not in ids and uid in {m["user_id"] for m in members}:
            ids.append(uid)
        await draft_set("man", ids)
        await cb.answer()
        return await draft_show(cb)

    if data == "st:undo":
        await draft_set("man", ids[:-1])
        await cb.answer()
        return await draft_show(cb)

    if data == "st:fill":
        rest = [m["user_id"] for m in members if m["user_id"] not in ids]
        random.shuffle(rest)
        await draft_set("man", ids + rest)
        await cb.answer("🎲 Qolganlar aralashtirildi")
        return await draft_show(cb)

    if data == "st:ok":
        await cb.answer()
        return await apply_order(cb, ids)


async def apply_order(cb: CallbackQuery, ids: list[int]):
    bot = cb.bot
    members = await active_members()
    valid = {m["user_id"] for m in members}
    ids = [i for i in ids if i in valid]
    ids += [m["user_id"] for m in members if m["user_id"] not in ids]  # tanlov paytida qo'shilganlar oxiriga
    if not ids:
        return await show(cb, "Ro'yxat bo'sh.", panel_kb(await is_started()))

    for i, uid in enumerate(ids, 1):
        await pool.execute("UPDATE members SET pos=$2 WHERE user_id=$1", uid, i)
    # chiqarilgan/nofaol a'zolar oxirida qoladi
    others = await pool.fetch(
        "SELECT user_id FROM members WHERE NOT (user_id = ANY($1::bigint[])) ORDER BY pos, seq", ids
    )
    for j, r in enumerate(others, len(ids) + 1):
        await pool.execute("UPDATE members SET pos=$2 WHERE user_id=$1", r["user_id"], j)
    await draft_set("", [])

    was_started = await is_started()
    now = datetime.now(TZ)
    today = shift_day(now)

    if not was_started:
        await pool.execute("DELETE FROM shift_photos WHERE day IN (SELECT day FROM shifts)")
        await pool.execute("DELETE FROM shifts")
        await set_setting("start_day", today.isoformat())
        await set_setting("started", "1")
        row = await get_or_create_shift(today)
    else:
        row = await pool.fetchrow("SELECT * FROM shifts WHERE day=$1", today)
        if row:
            await pool.execute(
                "UPDATE shifts SET seq=(SELECT pos FROM members WHERE user_id=shifts.user_id) WHERE day=$1", today
            )
        row = await get_or_create_shift(today)

    mem = {m["user_id"]: m for m in await active_members()}
    order_lines = "\n".join(f"{i}. {who(mem[u])}" for i, u in enumerate(ids, 1) if u in mem)

    if was_started:
        await show(cb, f"✅ <b>Tartib saqlandi</b>\n{LINE}\n{order_lines}", panel_kb(True))
        return

    await show(
        cb,
        f"🚀 <b>Navbatchilik boshlandi!</b>\n{LINE}\n{order_lines}\n\n"
        f"📅 Bugungi navbatchi: <b>{who(row)}</b>",
        panel_kb(True),
    )
    chat = await get_chat_id()
    if chat:
        await safe_send(
            bot, chat,
            f"🚀 <b>Navbatchilik boshlandi!</b>\n{LINE}\n<b>Navbat tartibi:</b>\n{order_lines}\n\n"
            f"📅 Bugungi navbatchi: <b>{who_group(row)}</b>",
        )
    if row:
        await safe_send(
            bot, row["user_id"],
            f"📌 {who(row)}, navbatchilik boshlandi va bugun navbat <b>sizda</b>!\n"
            "Bajarib bo'lgach «Bajardim» tugmasini bosing.",
            reply_markup=bajardim_kb(row["day"]),
        )


# ---- eski buyruqlar (saqlab qolingan)
@router.message(Command("vaqtlar"))
async def cmd_times(message: Message, command: CommandObject):
    if not await admin_only(message):
        return
    if not command.args:
        cur = ", ".join(f"{h:02d}:{m:02d}" for h, m in await get_day_times()) or "yo'q"
        return await message.reply(f"Kunduzgi eslatma vaqtlari: {cur}\nO'zgartirish: /vaqtlar 09:00 13:00 17:00")
    parts = re.split(r"[\s,]+", command.args.strip())
    ok = []
    for p in parts:
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", p)
        if not m:
            return await message.reply(f"«{html.escape(p)}» noto'g'ri. Format: 09:00")
        h, mi = int(m[1]), int(m[2])
        if not valid_day_time(h, mi):
            return await message.reply(
                f"«{html.escape(p)}» mumkin emas. Kunduzgi vaqt 02:32 dan 18:59 gacha bo'lishi kerak "
                f"(kechki eslatmalar 19:00 dan boshlanadi)."
            )
        ok.append(f"{h:02d}:{mi:02d}")
    await set_setting("day_times", ",".join(ok))
    await message.reply("✅ Saqlandi: " + ", ".join(ok))


async def _set_excluded(message: Message, command: CommandObject, value: bool):
    if not await admin_only(message):
        return
    rows = await all_members()
    uid = None
    try:
        idx = int((command.args or "").strip()) - 1
        if 0 <= idx < len(rows):
            uid = rows[idx]["user_id"]
    except ValueError:
        pass
    if not uid:
        return await message.reply("Raqamni yozing: /chiqar 3 (raqam /azolar da ko'rinadi)")
    await pool.execute(
        "UPDATE members SET excluded=$2::boolean, active=NOT $2::boolean WHERE user_id=$1", uid, value
    )
    await message.reply("✅ Navbatdan chiqarildi." if value else "✅ Navbatga qaytarildi.")


@router.message(Command("chiqar"))
async def cmd_exclude(message: Message, command: CommandObject):
    await _set_excluded(message, command, True)


@router.message(Command("qaytar"))
async def cmd_include(message: Message, command: CommandObject):
    await _set_excluded(message, command, False)


async def do_skip(bot: Bot):
    """Navbatchini keyingisiga o'tkazadi. (matn, ok) qaytaradi."""
    if not await is_started():
        return "Navbatchilik hali boshlanmagan.", False
    day = shift_day(datetime.now(TZ))
    row = await get_or_create_shift(day)
    if not row:
        return "Ro'yxat bo'sh.", False
    if row["status"] in ("review", "approved"):
        return "Bu smena allaqachon topshirilgan, o'tkazib bo'lmaydi.", False
    members = await active_members()
    nxt = next_after(members, row["seq"])
    if not nxt:
        return "Navbatda faol a'zo qolmadi.", False
    await pool.execute(
        "UPDATE shifts SET user_id=$2, username=$3, full_name=$4, seq=$5, status='open' WHERE day=$1",
        day, nxt["user_id"], nxt["username"], nxt["full_name"], nxt["pos"],
    )
    await pool.execute("DELETE FROM shift_photos WHERE day=$1", day)
    await safe_send(bot, nxt["user_id"], f"📌 {who(nxt)}, navbatchilik sizga o'tkazildi.",
                    reply_markup=bajardim_kb(day))
    return f"🔁 Navbat o'tkazildi. Yangi navbatchi: <b>{who(nxt)}</b>", True


@router.message(Command("otkazish"))
async def cmd_skip(message: Message):
    if not await admin_only(message):
        return
    text, _ = await do_skip(message.bot)
    await message.reply(text)


@router.callback_query(F.data == "adm:skip")
async def cb_skip(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Faqat super admin uchun.", show_alert=True)
    await cb.answer()
    await show(cb, "🔁 Bugungi navbatchini keyingisiga o'tkazamizmi?",
               kb([("✅ Ha, o'tkazish", "adm:skipok"), ("⬅️ Yo'q", "adm:panel")]))


@router.callback_query(F.data == "adm:skipok")
async def cb_skip_ok(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Faqat super admin uchun.", show_alert=True)
    text, _ = await do_skip(cb.bot)
    await cb.answer()
    await show(cb, text, kb([("👑 Admin paneli", "adm:panel")]))


# ---- bugungi navbatchini bekor qilib, boshqa odamni tayinlash
@router.callback_query(F.data == "adm:assign")
async def cb_assign(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Faqat super admin uchun.", show_alert=True)
    if not await is_started():
        return await cb.answer("Navbatchilik hali boshlanmagan.", show_alert=True)
    row = await get_or_create_shift(shift_day(datetime.now(TZ)))
    if not row:
        return await cb.answer("Ro'yxat bo'sh.", show_alert=True)
    members = await active_members()
    rows, line = [], []
    for m in members:
        label = m["full_name"] if len(m["full_name"]) <= 18 else m["full_name"][:17] + "…"
        mark = "👉 " if m["user_id"] == row["user_id"] else ""
        line.append((f"{mark}{label}", f"as:{m['user_id']}"))
        if len(line) == 2:
            rows.append(line)
            line = []
    if line:
        rows.append(line)
    rows.append([("⬅️ Orqaga", "adm:panel")])
    await cb.answer()
    await show(
        cb,
        f"👤 <b>Navbatchini almashtirish</b>\n{LINE}\n"
        f"Hozirgi: <b>{who(row)}</b> — {STATUS_TEXT[row['status']]}\n\n"
        "Bugun uchun yangi navbatchini tanlang. Joriy navbatchilik (rasmlar, tasdiq) bekor qilinadi:",
        kb(*rows),
    )


@router.callback_query(F.data.startswith("as:"))
async def cb_assign_pick(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Faqat super admin uchun.", show_alert=True)
    uid = int(cb.data.split(":", 1)[1])
    m = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", uid)
    if not m:
        return await cb.answer("A'zo topilmadi.", show_alert=True)
    await cb.answer()
    await show(
        cb,
        f"❓ Bugungi navbatchilik <b>{who(m)}</b> ga tayinlansinmi?\n\n"
        "Hozirgi navbatchining natijasi bekor qilinadi, yangi odamga xabar boradi.",
        kb([("✅ Ha, tayinlash", f"asok:{uid}"), ("⬅️ Yo'q", "adm:assign")]),
    )


@router.callback_query(F.data.startswith("asok:"))
async def cb_assign_ok(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Faqat super admin uchun.", show_alert=True)
    uid = int(cb.data.split(":", 1)[1])
    new = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1 AND active AND NOT excluded", uid)
    if not new:
        return await cb.answer("Bu a'zo navbatda emas.", show_alert=True)
    day = shift_day(datetime.now(TZ))
    old = await get_or_create_shift(day)
    if not old:
        return await cb.answer("Ro'yxat bo'sh.", show_alert=True)
    if old["user_id"] == uid and old["status"] in ("open", "collecting"):
        return await cb.answer("U allaqachon bugungi navbatchi.", show_alert=True)

    await pool.execute(
        "UPDATE shifts SET user_id=$2, username=$3, full_name=$4, seq=$5, status='open', admin_comment=NULL "
        "WHERE day=$1",
        day, new["user_id"], new["username"], new["full_name"], new["pos"],
    )
    await pool.execute("DELETE FROM shift_photos WHERE day=$1", day)
    # shu smena uchun kutilayotgan izoh so'rovi bo'lsa, bekor qilamiz
    pend = await get_setting("admin_pending")
    if pend and pend.split("|")[1] == day.isoformat():
        await set_setting("admin_pending", "")

    await cb.answer("Almashtirildi ✅")
    await show(
        cb,
        f"✅ <b>Navbatchi almashtirildi</b>\n{LINE}\nEndi bugungi navbatchi: <b>{who(new)}</b>",
        kb([("👑 Admin paneli", "adm:panel")]),
    )
    if old["user_id"] != uid:
        await safe_send(cb.bot, old["user_id"],
                        f"ℹ️ {who(old)}, bugungi navbatchilik sizdan olindi va boshqa odamga tayinlandi.")
    await safe_send(
        cb.bot, new["user_id"],
        f"📌 {who(new)}, bugungi navbatchilik <b>sizga tayinlandi</b>.\n"
        "Bajarib bo'lgach «Bajardim» tugmasini bosing.",
        reply_markup=bajardim_kb(day),
    )
    chat = await get_chat_id()
    if chat:
        await safe_send(cb.bot, chat,
                        f"🔄 Bugungi navbatchi almashtirildi.\n📅 Yangi navbatchi: <b>{who_group(new)}</b>")


# ------------------------------------------- navbatchi: bajardim -> rasmlar -> yuborish
async def start_collecting(bot: Bot, row) -> bool:
    msg = await safe_send(
        bot, row["user_id"],
        f"📸 {who(row)}, bajarilgan ish rasmlarini yuboring (1–{MAX_PHOTOS} ta).\n"
        f"Rasmlarni <b>fayl sifatida emas</b>, oddiy rasm qilib yuboring.\n"
        f"Tugagach «📤 Adminga yuborish» tugmasini bosing.",
        reply_markup=send_kb(row["day"]),
    )
    if msg is None:
        return False
    await pool.execute("UPDATE shifts SET status='collecting' WHERE day=$1", row["day"])
    return True


@router.message(Command("bajardim"), PRIVATE)
async def cmd_bajardim(message: Message):
    row = await pool.fetchrow(
        "SELECT * FROM shifts WHERE user_id=$1 AND status IN ('open','collecting') ORDER BY day DESC LIMIT 1",
        message.from_user.id,
    )
    if not row:
        return await message.answer("Hozir sizda ochiq navbatchilik yo'q.")
    await start_collecting(message.bot, row)


@router.callback_query(F.data.startswith("bajardim:"))
async def on_bajardim(cb: CallbackQuery):
    day = date.fromisoformat(cb.data.split(":", 1)[1])
    row = await pool.fetchrow("SELECT * FROM shifts WHERE day=$1", day)
    if not row:
        return await cb.answer("Topilmadi.", show_alert=True)
    if cb.from_user.id != row["user_id"]:
        return await cb.answer(f"Bu tugma faqat {plain(row)} uchun!", show_alert=True)
    if row["status"] == "review":
        return await cb.answer("Admin tekshirmoqda, kuting.", show_alert=True)
    if row["status"] == "approved":
        return await cb.answer("Allaqachon tasdiqlangan ✅", show_alert=True)
    if row["status"] == "missed":
        return await cb.answer("Vaqt tugagan.", show_alert=True)
    if await start_collecting(cb.bot, row):
        await cb.answer("Botdagi shaxsiy chatni oching, rasmlarni o'sha yerga yuboring 📸", show_alert=True)
    else:
        await cb.answer("Avval botga o'tib /start bosing.", show_alert=True)


@router.message(F.photo, PRIVATE)
async def on_photo(message: Message):
    row = await pool.fetchrow(
        "SELECT * FROM shifts WHERE user_id=$1 AND status='collecting' ORDER BY day DESC LIMIT 1",
        message.from_user.id,
    )
    if not row:
        return await message.reply(
            "Hozir rasm qabul qilinmaydi. Avval eslatmadagi «✅ Bajardim» tugmasini bosing yoki /bajardim yozing."
        )
    n = len(await photo_ids(row["day"]))
    if n >= MAX_PHOTOS:
        return await message.reply(f"Maksimum {MAX_PHOTOS} ta rasm. «📤 Adminga yuborish» tugmasini bosing.",
                                   reply_markup=send_kb(row["day"]))
    await pool.execute("INSERT INTO shift_photos(day,file_id) VALUES($1,$2)", row["day"], message.photo[-1].file_id)
    await message.reply(f"📸 {n + 1}-rasm qabul qilindi.", reply_markup=send_kb(row["day"]))


@router.message(F.document, PRIVATE)
async def on_document(message: Message):
    await message.reply("📎 Fayl qabul qilinmaydi. Iltimos, rasmni «Fayl» sifatida emas, oddiy rasm qilib yuboring.")


@router.callback_query(F.data.startswith("send:"))
async def on_send(cb: CallbackQuery):
    day = date.fromisoformat(cb.data.split(":", 1)[1])
    row = await pool.fetchrow("SELECT * FROM shifts WHERE day=$1", day)
    if not row or cb.from_user.id != row["user_id"]:
        return await cb.answer("Bu tugma sizga tegishli emas.", show_alert=True)
    if row["status"] != "collecting":
        return await cb.answer("Allaqachon yuborilgan yoki yopilgan.", show_alert=True)
    files = await photo_ids(day)
    if not files:
        return await cb.answer("Avval kamida bitta rasm yuboring!", show_alert=True)
    admin = await get_admin_id()
    if not admin:
        return await cb.answer("Super admin belgilanmagan. Adminga xabar bering.", show_alert=True)

    # Super adminning o'zi navbatchi bo'lsa: o'zini o'zi tasdiqlamaydi, avtomatik tasdiqlanadi
    if row["user_id"] == admin:
        await pool.execute(
            "UPDATE shifts SET status='approved', admin_comment=$2 WHERE day=$1", day, "Admin o'zi bajardi"
        )
        await cb.answer("Navbatchilik qabul qilindi ✅")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await cb.message.answer("✅ Navbatchiligingiz qabul qilindi (admin sifatida avtomatik tasdiqlandi). Rahmat!")
        chat = await get_chat_id()
        if chat:
            await safe_send(cb.bot, chat, f"✅ {who_group(row)} navbatchilikni bajardi. Rahmat!")
        await announce_next(cb.bot, row)
        return

    await pool.execute("UPDATE shifts SET status='review' WHERE day=$1", day)
    caption = f"📋 <b>Navbatchilik hisoboti</b>\n👤 {who(row)}\n📅 {day.strftime('%d.%m.%Y')}"
    try:
        if len(files) == 1:
            await cb.bot.send_photo(admin, files[0], caption=caption)
        else:
            await cb.bot.send_media_group(
                admin,
                [
                    InputMediaPhoto(
                        media=f,
                        caption=caption if i == 0 else None,
                        parse_mode=ParseMode.HTML if i == 0 else None,
                    )
                    for i, f in enumerate(files)
                ],
            )
        await cb.bot.send_message(admin, f"{who(row)} navbatchilikni bajardi.\n<b>Rasmlarga qarab hukm qiling: bajarildimi?</b>",
                                  reply_markup=review_kb(day))
    except Exception:
        log.exception("adminga yuborib bo'lmadi")
        await pool.execute("UPDATE shifts SET status='collecting' WHERE day=$1", day)
        return await cb.answer("Adminga yuborib bo'lmadi. Admin botga /start bosganmi?", show_alert=True)

    await cb.answer("Adminga yuborildi ✅")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer("⏳ Hisobot adminga yuborildi. Tasdiqlashini kuting.")


# -------------------------------------------------- super admin: tasdiqlash
@router.callback_query(F.data.startswith("rv:"))
async def on_review(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer("Faqat super admin uchun.", show_alert=True)
    _, decision, d = cb.data.split(":")
    row = await pool.fetchrow("SELECT * FROM shifts WHERE day=$1", date.fromisoformat(d))
    if not row or row["status"] != "review":
        return await cb.answer("Bu hisobot allaqachon ko'rib chiqilgan.", show_alert=True)
    pend = await get_setting("admin_pending")
    if pend and pend.split("|")[1] != d:
        return await cb.answer(
            "Avval oldingi hisobot uchun izoh yozing yoki «Izohsiz» tugmasini bosing.", show_alert=True
        )
    await set_setting("admin_pending", f"{decision}|{d}")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    title = "✅ Bajarildi" if decision == "ok" else "❌ Bajarilmadi"
    await cb.message.answer(f"{title}. Izohni yozib yuboring yoki «Izohsiz» tugmasini bosing:",
                            reply_markup=kb([("Izohsiz", "rvskip")]))
    await cb.answer()


@router.callback_query(F.data == "rvskip")
async def on_review_skip(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id):
        return await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer()
    await finalize_review(cb.bot, None)


async def announce_next(bot: Bot, row):
    """Navbatchilik bajarildi deb tasdiqlangach, ro'yxatdagi keyingi odamni e'lon qiladi."""
    nxt = next_after(await active_members(), row["seq"])
    if not nxt or nxt["user_id"] == row["user_id"]:
        return ""
    when = (row["day"] + timedelta(days=1)).strftime("%d.%m.%Y")
    chat = await get_chat_id()
    if chat:
        await safe_send(bot, chat, f"➡️ Keyingi navbatchi: <b>{who_group(nxt)}</b> ({when})")
    await safe_send(bot, nxt["user_id"],
                    f"📌 {who(nxt)}, keyingi navbatchilik <b>sizda</b> ({when}). Tayyor bo'ling!")
    return f"\n➡️ Keyingi navbatchi: <b>{who(nxt)}</b> ({when})"


async def finalize_review(bot: Bot, comment: str | None):
    pend = await get_setting("admin_pending")
    if not pend:
        return
    decision, d = pend.split("|")
    day = date.fromisoformat(d)
    row = await pool.fetchrow("SELECT * FROM shifts WHERE day=$1", day)
    await set_setting("admin_pending", "")
    admin = await get_admin_id()
    if not row:
        return
    note = f"\n💬 Izoh: {html.escape(comment)}" if comment else ""

    if decision == "ok":
        await pool.execute("UPDATE shifts SET status='approved', admin_comment=$2 WHERE day=$1", day, comment)
        await safe_send(bot, row["user_id"], f"✅ {who(row)}, navbatchiligingiz <b>bajarildi</b> deb tasdiqlandi. Rahmat!{note}")
        chat = await get_chat_id()
        if chat:
            await safe_send(bot, chat, f"✅ {who_group(row)} navbatchilikni bajardi. Admin tasdiqladi. Rahmat!")
        nxt_line = await announce_next(bot, row)
        if admin:
            await safe_send(bot, admin, f"✅ Bajarildi deb tasdiqlandi.{nxt_line}")
    else:
        await pool.execute("UPDATE shifts SET status='open', admin_comment=$2 WHERE day=$1", day, comment)
        await pool.execute("DELETE FROM shift_photos WHERE day=$1", day)
        await safe_send(
            bot, row["user_id"],
            f"❌ {who(row)}, navbatchilik <b>bajarilmadi</b> deb topildi.{note}\n\n"
            f"Iltimos, qaytadan bajarib, «✅ Bajardim» tugmasini bosing va yangi rasmlar yuboring.",
            reply_markup=bajardim_kb(day),
        )
        if admin:
            await safe_send(bot, admin, "❌ Bajarilmadi deb belgilandi. Navbatchi qayta bajaradi, unga xabar yuborildi.")


# ---------------------------------------------------------------- scheduler
async def tick(bot: Bot, now: datetime, hm, day_times, is_day: bool, is_eve: bool, is_final: bool):
    if not await is_started():
        return  # navbatchilik boshlanmaguncha eslatma yo'q
    row = await get_or_create_shift(shift_day(now))
    if not row:
        return
    chat = await get_chat_id()
    admin = await get_admin_id()

    if is_final:
        stale = await pool.fetch(
            "SELECT * FROM shifts WHERE status IN ('open','collecting') AND day<=$1 ORDER BY day", row["day"]
        )
        for r in stale:
            await pool.execute("UPDATE shifts SET status='missed' WHERE day=$1", r["day"])
            when = "bugungi" if r["day"] == row["day"] else f"{r['day'].strftime('%d.%m.%Y')} dagi"
            if chat:
                await safe_send(bot, chat, f"⚠️ {who_group(r)} {when} navbatchilikni bajarmadi.")
            if admin:
                await safe_send(bot, admin, f"⚠️ {who(r)} {when} navbatchilikni bajarmadi.")
        return

    if row["status"] not in ("open", "collecting"):
        return

    if is_eve:
        await safe_send(bot, row["user_id"], random.choice(EVE_TEXTS).format(who=who(row)),
                        reply_markup=bajardim_kb(row["day"]))
        if chat and hm in GROUP_SLOTS:
            await safe_send(bot, chat, random.choice(EVE_TEXTS).format(who=who_group(row)),
                            reply_markup=bajardim_kb(row["day"]))
    elif is_day:
        if chat and hm == day_times[0]:
            await safe_send(bot, chat, f"📅 Bugungi navbatchi: <b>{who_group(row)}</b>")
        await safe_send(bot, row["user_id"], random.choice(DAY_TEXTS).format(who=who(row)),
                        reply_markup=bajardim_kb(row["day"]))


async def scheduler(bot: Bot):
    last = None
    while True:
        try:
            now = datetime.now(TZ)
            hm = (now.hour, now.minute)
            key = (now.date(), hm)
            if key != last:
                last = key
                day_times = await get_day_times()
                is_day, is_eve, is_final = hm in day_times, hm in REM_SLOTS, hm == FINAL_SLOT
                if is_day or is_eve or is_final:
                    await tick(bot, now, hm, day_times, is_day, is_eve, is_final)
        except Exception:
            log.exception("scheduler xatosi")
        await asyncio.sleep(15)


# ------------------------------------------------------ Render health check
async def health(_request):
    return web.Response(text="ok")


async def start_web():
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()


async def keep_alive():
    if not SELF_URL:
        return
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(600)
            try:
                async with session.get(SELF_URL) as resp:
                    await resp.read()
            except Exception as e:
                log.warning("keep_alive xatosi: %s", e)


async def migrate():
    # eski a'zolar uchun tartib raqami
    await pool.execute("UPDATE members SET pos=seq WHERE pos IS NULL")
    # eski (allaqachon ishlayotgan) bot: smenalar bor bo'lsa, "boshlangan" deb hisoblaymiz
    if await get_setting("started") is None:
        has = await pool.fetchval("SELECT EXISTS(SELECT 1 FROM shifts)")
        await set_setting("started", "1" if has else "0")


async def main():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await pool.execute(SCHEMA)
    await migrate()
    if SUPER_ADMIN_ID:
        await set_setting("admin_id", SUPER_ADMIN_ID)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    await start_web()
    await bot.delete_webhook(drop_pending_updates=False)
    background_tasks.append(asyncio.create_task(scheduler(bot)))
    background_tasks.append(asyncio.create_task(keep_alive()))
    await dp.start_polling(bot, allowed_updates=["message", "callback_query", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
