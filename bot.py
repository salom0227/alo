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
SUPER_ADMIN_ID = os.getenv("SUPER_ADMIN_ID")  # ixtiyoriy: admin Telegram ID si (bo'lsa, doim shu admin)
ADMIN_CODE = os.getenv("ADMIN_CODE")  # ixtiyoriy: /admin KOD orqali admin bo'lish (SUPER_ADMIN_ID yo'q bo'lsa)
PORT = int(os.getenv("PORT", "10000"))
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Tashkent"))
SELF_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render o'zi beradi; o'zini uyg'otib turish uchun

# Kechki smena 19:00 da boshlanadi va ertasi kuni 02:30 gacha davom etadi.
# Navbatchiga (shaxsiy chatga) har 30 daqiqada eslatma: 19:00 ... 02:30. 02:31 da yakuniy tekshiruv.
REM_SLOTS = {(h, m) for h in (19, 20, 21, 22, 23, 0, 1) for m in (0, 30)} | {(2, 0), (2, 30)}
# Guruhga esa kamroq (ko'p xabar bo'lmasligi uchun):
GROUP_SLOTS = {(19, 0), (21, 0), (23, 0), (1, 0), (2, 30)}
FINAL_SLOT = (2, 31)
EVE_START = (19, 0)
DEFAULT_DAY_TIMES = "09:00,13:00,17:00"  # /vaqtlar bilan o'zgartiriladi
MAX_PHOTOS = 10
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


# ------------------------------------------------------------------ helpers
def shift_day(now: datetime) -> date:
    """02:31 gacha bo'lgan vaqt kechagi smenaga tegishli."""
    if (now.hour, now.minute) <= FINAL_SLOT:
        return now.date() - timedelta(days=1)
    return now.date()


def who(r) -> str:
    """Ism va username (DM uchun)."""
    s = html.escape(r["full_name"])
    return f"{s} (@{r['username']})" if r["username"] else s


def who_group(r) -> str:
    """Guruhda ping qilish uchun: ism + @username yoki havola."""
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
    return kb([("✅ Ha, tasdiqlayman", f"rv:ok:{d}"), ("❌ Yo'q", f"rv:no:{d}")])


async def safe_send(bot: Bot, chat_id: int, text: str, **kw):
    try:
        return await bot.send_message(chat_id, text, **kw)
    except Exception as e:
        log.warning("yuborib bo'lmadi (%s): %s", chat_id, e)
        return None


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


def valid_day_time(h: int, m: int) -> bool:
    """Kunduzgi eslatma: 02:31 dan keyin va kechki eslatmalar (19:00) boshlanishidan oldin."""
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
        INSERT INTO members(user_id, username, full_name) VALUES($1,$2,$3)
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
    return await pool.fetch("SELECT * FROM members WHERE active AND NOT excluded ORDER BY seq")


def next_after(members, seq):
    if not members:
        return None
    if seq is None:
        return members[0]
    for m in members:
        if m["seq"] > seq:
            return m
    return members[0]


async def get_or_create_shift(day: date):
    row = await pool.fetchrow("SELECT * FROM shifts WHERE day=$1", day)
    if row:
        return row
    members = await active_members()
    if not members:
        return None
    prev = await pool.fetchrow("SELECT seq FROM shifts WHERE day<$1 ORDER BY day DESC LIMIT 1", day)
    pick = next_after(members, prev["seq"] if prev else None)
    await pool.execute(
        "INSERT INTO shifts(day,user_id,username,full_name,seq) VALUES($1,$2,$3,$4,$5) "
        "ON CONFLICT DO NOTHING",
        day,
        pick["user_id"],
        pick["username"],
        pick["full_name"],
        pick["seq"],
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
        return  # admin qilinganda qayta salomlashmasin
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


@router.message(Command("start", "yordam"), PRIVATE)
async def cmd_start(message: Message):
    u = message.from_user
    exists = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", u.id)
    if exists:
        await register_member(u)  # username o'zgargan bo'lsa yangilanadi
        exists = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", u.id)
        text = f"Salom, {who(exists)}! Siz navbatchilar ro'yxatidasiz ✅\n\n"
    elif u.username:
        await register_member(u)
        row = await pool.fetchrow("SELECT * FROM members WHERE user_id=$1", u.id)
        text = f"✅ Ro'yxatdan o'tdingiz: {who(row)}\n\n"
        await notify_admin_new(message.bot, row)
    else:
        await set_setting(f"await:{u.id}", "1")
        return await message.answer(
            "Salom! Sizda Telegram username yo'q ekan.\n✏️ Iltimos, ism-familiyangizni yozib yuboring."
        )
    text += (
        "/bugun — joriy navbatchi\n"
        "/azolar — navbat tartibi\n"
        "/bajardim — navbatchilikni topshirish (navbatchi bo'lsangiz)\n"
        "/ism — ismni o'zgartirish"
    )
    if await is_admin(u.id):
        text += (
            "\n\n<b>Super admin:</b>\n"
            "/ulash — guruhda yozing, bot shu guruhga ulanadi\n"
            "/otkazish — navbatchini keyingisiga o'tkazish\n"
            "/chiqar N — a'zoni navbatdan chiqarish (N — /azolar dagi raqam)\n"
            "/qaytar N — navbatga qaytarish\n"
            "/vaqtlar 09:00 13:00 17:00 — kunduzgi eslatma vaqtlari"
        )
    await message.answer(text)


async def notify_admin_new(bot: Bot, row):
    admin = await get_admin_id()
    if admin and admin != row["user_id"]:
        await safe_send(bot, admin, f"🆕 Yangi a'zo ro'yxatdan o'tdi: {who(row)}")


@router.message(Command("ism"), PRIVATE)
async def cmd_name(message: Message):
    await set_setting(f"await:{message.from_user.id}", "1")
    await message.answer("✏️ Yangi ism-familiyangizni yozib yuboring.")


@router.message(Command("admin"), PRIVATE)
async def cmd_admin(message: Message, command: CommandObject):
    if SUPER_ADMIN_ID:
        return await message.answer("Admin SUPER_ADMIN_ID orqali belgilangan, kod bilan o'zgartirib bo'lmaydi.")
    if ADMIN_CODE and (command.args or "").strip() == ADMIN_CODE:
        await set_setting("admin_id", str(message.from_user.id))
        return await message.answer("👑 Siz endi super adminsiz.")
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
        await message.answer(f"✅ Saqlandi: {who(row)}\n/bugun — joriy navbatchi")
        if not member:
            await notify_admin_new(message.bot, row)
        return

    if await is_admin(uid) and await get_setting("admin_pending"):
        return await finalize_review(message.bot, message.text.strip())

    if not member:
        await message.answer("Ro'yxatdan o'tish uchun /start bosing.")


# --------------------------------------------------------------- umumiy buyruqlar
@router.message(Command("bugun"))
async def cmd_today(message: Message):
    row = await get_or_create_shift(shift_day(datetime.now(TZ)))
    if not row:
        return await message.reply("Ro'yxat bo'sh. Botga /start bosib ro'yxatdan o'ting.")
    await message.reply(f"Navbatchi: <b>{who(row)}</b> — {STATUS_TEXT[row['status']]}")


@router.message(Command("azolar"))
async def cmd_members(message: Message):
    rows = await pool.fetch("SELECT * FROM members ORDER BY seq")
    if not rows:
        return await message.reply("Ro'yxat hali bo'sh.")
    cur = await pool.fetchrow("SELECT user_id FROM shifts WHERE day=$1", shift_day(datetime.now(TZ)))
    cur_id = cur["user_id"] if cur else None
    lines = []
    for i, m in enumerate(rows, 1):
        mark = "⛔" if (m["excluded"] or not m["active"]) else ("👉" if m["user_id"] == cur_id else "▫️")
        lines.append(f"{mark} {i}. {who(m)}")
    await message.reply("<b>Navbat tartibi:</b>\n" + "\n".join(lines))


# -------------------------------------------------------- admin buyruqlari
async def admin_only(message: Message) -> bool:
    if not await is_admin(message.from_user.id):
        await message.reply("Faqat super admin uchun.")
        return False
    return True


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
    rows = await pool.fetch("SELECT user_id FROM members ORDER BY seq")
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


@router.message(Command("otkazish"))
async def cmd_skip(message: Message):
    if not await admin_only(message):
        return
    day = shift_day(datetime.now(TZ))
    row = await get_or_create_shift(day)
    if not row:
        return await message.reply("Ro'yxat bo'sh.")
    if row["status"] in ("review", "approved"):
        return await message.reply("Bu smena allaqachon topshirilgan, o'tkazib bo'lmaydi.")
    members = await active_members()
    nxt = next_after(members, row["seq"])
    if not nxt:
        return await message.reply("Navbatda faol a'zo qolmadi.")
    await pool.execute(
        "UPDATE shifts SET user_id=$2, username=$3, full_name=$4, seq=$5, status='open' WHERE day=$1",
        day, nxt["user_id"], nxt["username"], nxt["full_name"], nxt["seq"],
    )
    await pool.execute("DELETE FROM shift_photos WHERE day=$1", day)
    await message.reply(f"🔁 Navbat o'tkazildi. Yangi navbatchi: {who(nxt)}")
    await safe_send(message.bot, nxt["user_id"], f"📌 {who(nxt)}, navbatchilik sizga o'tkazildi.",
                    reply_markup=bajardim_kb(day))


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
        await cb.bot.send_message(admin, f"{who(row)} navbatchilikni bajardi.\n<b>Tasdiqlaysizmi?</b>",
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
    title = "✅ Tasdiqlanmoqda" if decision == "ok" else "❌ Rad etilmoqda"
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
        await safe_send(bot, row["user_id"], f"✅ {who(row)}, navbatchiligingiz tasdiqlandi. Rahmat!{note}")
        chat = await get_chat_id()
        if chat:
            await safe_send(bot, chat, f"✅ {who_group(row)} navbatchilikni bajardi. Admin tasdiqladi. Rahmat!")
        if admin:
            await safe_send(bot, admin, "✅ Tasdiqlandi.")
    else:
        await pool.execute("UPDATE shifts SET status='open', admin_comment=$2 WHERE day=$1", day, comment)
        await pool.execute("DELETE FROM shift_photos WHERE day=$1", day)
        await safe_send(
            bot, row["user_id"],
            f"❌ {who(row)}, navbatchilik qabul qilinmadi.{note}\n\n"
            f"Iltimos, qaytadan bajarib, «✅ Bajardim» tugmasini bosing.",
            reply_markup=bajardim_kb(day),
        )
        if admin:
            await safe_send(bot, admin, "❌ Rad etildi, navbatchiga xabar yuborildi.")


# ---------------------------------------------------------------- scheduler
async def tick(bot: Bot, now: datetime, hm, day_times, is_day: bool, is_eve: bool, is_final: bool):
    row = await get_or_create_shift(shift_day(now))
    if not row:
        return
    chat = await get_chat_id()
    admin = await get_admin_id()

    if is_final:
        # Joriy va barcha eskirgan, hali yopilmagan smenalarni yopamiz
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
        return  # topshirilgan yoki tekshiruvda: eslatma kerak emas

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
                last = key  # xato bo'lsa ham bir daqiqada qayta-qayta yubormaslik uchun
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
    """Render bepul tarifida uxlab qolmaslik uchun o'ziga har 10 daqiqada so'rov yuboradi."""
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


async def main():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    await pool.execute(SCHEMA)
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
