import asyncio
import html
import json
import logging
import os
import secrets
import time
import uuid
 
import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
 
load_dotenv()
logging.basicConfig(level=logging.INFO)
 
# ───────────────────────── تنظیمات ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ["ADMIN_IDS"].split(",") if x.strip()}
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "your_support")
 
CARD_NUMBER = os.getenv("CARD_NUMBER", "6037-0000-0000-0000")
CARD_HOLDER = os.getenv("CARD_HOLDER", "نام صاحب کارت")
 
XUI_URL = os.environ["XUI_URL"].rstrip("/")  # مثلا https://panel.example.com:2053/path
XUI_USER = os.environ["XUI_USER"]
XUI_PASS = os.environ["XUI_PASS"]
INBOUND_ID = int(os.environ["INBOUND_ID"])
SUB_URL = os.environ["SUB_URL"].rstrip("/")  # مثلا https://panel.example.com:2096/sub
 
# پلن‌ها: قیمت به تومان
PLANS = [
    {"name": "1 ماهه - 10 گیگ", "gb": 30, "days": 30, "price": 750_000},
    {"name": "۱ ماهه - 20 گیگ", "gb": 60, "days": 30, "price": 150_000},
    {"name": "1 ماهه - 30 گیگ", "gb": 100, "days": 90, "price": 220_000},
    {"name": "1 ماهه - 50 گیگ", "gb": 0, "days": 90, "price": 300_000},
]
 
DB_PATH = os.getenv("DB_PATH", "bot.db")
 
 
# ───────────────────────── دیتابیس ─────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, name TEXT, created INTEGER);
            CREATE TABLE IF NOT EXISTS services(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                email TEXT, uuid TEXT, sub_id TEXT, plan_name TEXT, created INTEGER);
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                plan_idx INTEGER, price INTEGER, service_id INTEGER,
                status TEXT, receipt TEXT, created INTEGER);
            """
        )
        await db.commit()
 
 
async def q_one(q, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, args)
        return await cur.fetchone()
 
 
async def q_all(q, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(q, args)
        return await cur.fetchall()
 
 
async def q_run(q, args=()):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q, args)
        await db.commit()
        return cur.rowcount, cur.lastrowid
 
 
# ───────────────────────── کلاینت 3x-ui ─────────────────────────
class XUI:
    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()
 
    async def _login(self):
        if self.session:
            await self.session.close()
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        async with self.session.post(
            f"{XUI_URL}/login", data={"username": XUI_USER, "password": XUI_PASS}
        ) as r:
            data = await r.json(content_type=None)
            if not data.get("success"):
                raise RuntimeError(f"3x-ui login failed: {data}")
 
    async def request(self, method: str, path: str, **kw) -> dict:
        async with self.lock:
            if self.session is None:
                await self._login()
            for attempt in range(2):
                async with self.session.request(method, XUI_URL + path, **kw) as r:
                    # سشن منقضی‌شده در 3x-ui معمولا 404 برمی‌گردونه
                    if r.status == 404 and attempt == 0:
                        await self._login()
                        continue
                    data = await r.json(content_type=None)
                    if not data.get("success"):
                        raise RuntimeError(f"3x-ui error: {data}")
                    return data
            raise RuntimeError("3x-ui request failed")
 
    @staticmethod
    def _client_payload(uid, email, sub_id, gb, days):
        expiry = int((time.time() + days * 86400) * 1000) if days else 0
        client = {
            "id": uid,
            "flow": "",
            "email": email,
            "limitIp": 0,
            "totalGB": int(gb * 1024**3),
            "expiryTime": expiry,
            "enable": True,
            "tgId": "",
            "subId": sub_id,
            "comment": "",
            "reset": 0,
        }
        return {"id": INBOUND_ID, "settings": json.dumps({"clients": [client]})}
 
    async def add_client(self, email, gb, days):
        uid = str(uuid.uuid4())
        sub_id = secrets.token_hex(8)
        await self.request(
            "POST",
            "/panel/api/inbounds/addClient",
            data=self._client_payload(uid, email, sub_id, gb, days),
        )
        return uid, sub_id
 
    async def renew_client(self, uid, email, sub_id, gb, days):
        await self.request(
            "POST",
            f"/panel/api/inbounds/updateClient/{uid}",
            data=self._client_payload(uid, email, sub_id, gb, days),
        )
        await self.request(
            "POST", f"/panel/api/inbounds/{INBOUND_ID}/resetClientTraffic/{email}"
        )
 
    async def traffic(self, email):
        data = await self.request("GET", f"/panel/api/inbounds/getClientTraffics/{email}")
        return data.get("obj")
 
 
xui = XUI()
 
 
# ───────────────────────── ابزارها ─────────────────────────
LINE = "━━━━━━━━━━━━━━━━"
NUM_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]
 
 
def fa(x) -> str:
    return str(x).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))
 
 
def fmt_price(p: int) -> str:
    return f"{fa(f'{p:,}')} تومان"
 
 
def fmt_gb(b: float) -> str:
    return f"{fa(f'{b / 1024**3:.2f}')} GB"
 
 
def bar(percent: float, n: int = 10) -> str:
    percent = max(0, min(100, percent))
    filled = round(percent / 100 * n)
    return "▰" * filled + "▱" * (n - filled)
 
 
def plan_volume(p) -> str:
    return "نامحدود ♾" if p["gb"] == 0 else f"{fa(p['gb'])} گیگابایت"
 
 
def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 خرید سرویس جدید", callback_data="buy")
    kb.button(text="📦 سرویس‌های من", callback_data="my")
    kb.button(text="📚 آموزش اتصال", callback_data="help")
    kb.button(text="💬 پشتیبانی", url=f"https://t.me/{SUPPORT_USERNAME}")
    kb.adjust(1, 2, 1)
    return kb.as_markup()
 
 
def plans_text(title: str) -> str:
    blocks = []
    for i, p in enumerate(PLANS):
        blocks.append(
            f"{NUM_EMOJI[i]} <b>{p['name']}</b>\n"
            f"     📊 {plan_volume(p)}  •  ⏳ {fa(p['days'])} روز\n"
            f"     💰 {fmt_price(p['price'])}"
        )
    return f"<b>{title}</b>\n{LINE}\n\n" + "\n\n".join(blocks) + f"\n\n{LINE}\n👇 یکی از پلن‌ها رو انتخاب کن"
 
 
def plans_kb(prefix: str, back: str = "menu"):
    kb = InlineKeyboardBuilder()
    for i, p in enumerate(PLANS):
        kb.button(text=f"{NUM_EMOJI[i]} {p['name']} • {fmt_price(p['price'])}", callback_data=f"{prefix}:{i}")
    kb.button(text="🔙 بازگشت", callback_data=back)
    kb.adjust(1)
    return kb.as_markup()
 
 
async def show(cb: CallbackQuery, text: str, markup=None):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        await cb.message.answer(text, reply_markup=markup)
 
 
class Pay(StatesGroup):
    waiting_receipt = State()
 
 
router = Router()
 
 
# ───────────────────────── کاربر ─────────────────────────
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await q_run(
        "INSERT OR IGNORE INTO users(id,name,created) VALUES(?,?,?)",
        (m.from_user.id, m.from_user.full_name, int(time.time())),
    )
    name = html.escape(m.from_user.first_name or "دوست عزیز")
    await m.answer(
        f"✨ سلام <b>{name}</b> عزیز، خوش اومدی!\n"
        f"{LINE}\n"
        "🚀 اینترنت پرسرعت و پایدار\n"
        "🔒 امن و بدون محدودیت\n"
        "⚡️ تحویل آنی بعد از تأیید پرداخت\n"
        "🛟 پشتیبانی همیشه در دسترس\n"
        f"{LINE}\n"
        "👇 از منوی زیر شروع کن:",
        reply_markup=main_menu(),
    )
 
 
@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "🏠 <b>منوی اصلی</b>\nیکی از گزینه‌ها رو انتخاب کن:", main_menu())
    await cb.answer()
 
 
@router.callback_query(F.data == "help")
async def help_(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 بازگشت", callback_data="menu")
    text = (
        "📚 <b>آموزش اتصال</b>\n"
        f"{LINE}\n"
        "<b>۱.</b> لینک اشتراک رو از بخش «📦 سرویس‌های من» کپی کن.\n\n"
        "<b>۲.</b> یکی از برنامه‌های زیر رو نصب کن:\n"
        "🤖 اندروید: Hiddify یا v2rayNG\n"
        "🍏 آیفون: Streisand یا Shadowrocket\n"
        "💻 ویندوز: Hiddify یا v2rayN\n\n"
        "<b>۳.</b> توی برنامه گزینه «افزودن از کلیپ‌بورد» یا Subscription رو بزن و لینک رو اضافه کن.\n\n"
        "<b>۴.</b> سرویس رو انتخاب و دکمه اتصال رو بزن ✅\n"
        f"{LINE}\n"
        "اگه به مشکل خوردی به پشتیبانی پیام بده 💬"
    )
    await show(cb, text, kb.as_markup())
    await cb.answer()
 
 
@router.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    await show(cb, plans_text("🛍 تعرفه‌های سرویس"), plans_kb("plan"))
    await cb.answer()
 
 
async def start_payment(cb: CallbackQuery, state: FSMContext, plan_idx: int, service_id=None):
    plan = PLANS[plan_idx]
    _, order_id = await q_run(
        "INSERT INTO orders(user_id,plan_idx,price,service_id,status,created) VALUES(?,?,?,?,?,?)",
        (cb.from_user.id, plan_idx, plan["price"], service_id, "awaiting_receipt", int(time.time())),
    )
    await state.set_state(Pay.waiting_receipt)
    await state.update_data(order_id=order_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ انصراف", callback_data="menu")
    kind = "تمدید سرویس" if service_id else "خرید سرویس"
    text = (
        f"🧾 <b>فاکتور #{fa(order_id)}</b> — {kind}\n"
        f"{LINE}\n"
        f"📦 پلن: <b>{plan['name']}</b>\n"
        f"📊 حجم: {plan_volume(plan)}\n"
        f"⏳ مدت: {fa(plan['days'])} روز\n"
        f"💰 مبلغ قابل پرداخت: <b>{fmt_price(plan['price'])}</b>\n"
        f"{LINE}\n"
        "💳 <b>اطلاعات پرداخت (کارت به کارت)</b>\n"
        f"<code>{CARD_NUMBER}</code>\n"
        f"👤 به نام: <b>{CARD_HOLDER}</b>\n"
        "<i>(برای کپی شماره کارت روش بزن)</i>\n"
        f"{LINE}\n"
        "📸 بعد از واریز، <b>عکس رسید</b> رو همینجا بفرست تا بررسی کنیم."
    )
    await show(cb, text, kb.as_markup())
    await cb.answer()
 
 
@router.callback_query(F.data.startswith("plan:"))
async def pick_plan(cb: CallbackQuery, state: FSMContext):
    await start_payment(cb, state, int(cb.data.split(":")[1]))
 
 
@router.callback_query(F.data.startswith("rp:"))
async def pick_renew_plan(cb: CallbackQuery, state: FSMContext):
    _, sid, idx = cb.data.split(":")
    await start_payment(cb, state, int(idx), service_id=int(sid))
 
 
@router.message(Pay.waiting_receipt, F.photo)
async def got_receipt(m: Message, state: FSMContext, bot: Bot):
    order_id = (await state.get_data())["order_id"]
    file_id = m.photo[-1].file_id
    await q_run("UPDATE orders SET status='pending', receipt=? WHERE id=?", (file_id, order_id))
    order = await q_one("SELECT * FROM orders WHERE id=?", (order_id,))
    plan = PLANS[order["plan_idx"]]
    kind = "🔄 تمدید" if order["service_id"] else "🆕 خرید جدید"
 
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید", callback_data=f"ok:{order_id}")
    kb.button(text="❌ رد", callback_data=f"no:{order_id}")
    kb.adjust(2)
    caption = (
        f"🔔 <b>سفارش جدید #{order_id}</b> — {kind}\n"
        f"{LINE}\n"
        f'👤 <a href="tg://user?id={m.from_user.id}">{html.escape(m.from_user.full_name)}</a>\n'
        f"🆔 <code>{m.from_user.id}</code>\n"
        f"📦 {plan['name']}\n"
        f"💰 {fmt_price(order['price'])}"
    )
    for admin in ADMIN_IDS:
        try:
            await bot.send_photo(admin, file_id, caption=caption, reply_markup=kb.as_markup())
        except Exception:
            logging.exception("failed to notify admin %s", admin)
    await state.clear()
    await m.answer(
        "✅ <b>رسید دریافت شد</b>\n"
        f"{LINE}\n"
        f"🧾 شماره سفارش: <b>#{fa(order_id)}</b>\n"
        "⏳ منتظر تأیید ادمین باش؛ به‌محض تأیید، سرویس برات ارسال میشه.",
        reply_markup=main_menu(),
    )
 
 
@router.message(Pay.waiting_receipt)
async def not_photo(m: Message):
    await m.answer("📸 لطفاً فقط <b>عکس رسید</b> رو بفرست (یا برای انصراف /start بزن).")
 
 
@router.callback_query(F.data == "my")
async def my_services(cb: CallbackQuery):
    rows = await q_all("SELECT * FROM services WHERE user_id=?", (cb.from_user.id,))
    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text="🛒 خرید سرویس", callback_data="buy")
        kb.button(text="🔙 بازگشت", callback_data="menu")
        kb.adjust(1)
        await show(cb, "📭 هنوز سرویسی نداری.\nبرای شروع یه پلن بخر 👇", kb.as_markup())
        return await cb.answer()
    for r in rows:
        kb.button(text=f"📦 {r['plan_name']}", callback_data=f"svc:{r['id']}")
    kb.button(text="🔙 بازگشت", callback_data="menu")
    kb.adjust(1)
    await show(cb, f"📦 <b>سرویس‌های من</b> ({fa(len(rows))})\nبرای دیدن جزئیات یکی رو انتخاب کن:", kb.as_markup())
    await cb.answer()
 
 
@router.callback_query(F.data.startswith("svc:"))
async def service_detail(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    svc = await q_one("SELECT * FROM services WHERE id=? AND user_id=?", (sid, cb.from_user.id))
    if not svc:
        return await cb.answer("سرویس پیدا نشد", show_alert=True)
    try:
        t = await xui.traffic(svc["email"])
    except Exception:
        logging.exception("traffic fetch failed")
        t = None
 
    if t:
        used = t["up"] + t["down"]
        expired = t["expiryTime"] != 0 and t["expiryTime"] / 1000 < time.time()
        exhausted = t["total"] != 0 and used >= t["total"]
        active = t.get("enable", True) and not expired and not exhausted
        status = "🟢 فعال" if active else "🔴 غیرفعال / تمام‌شده"
        if t["total"] == 0:
            usage = f"♾ نامحدود\n   مصرف تا الان: {fmt_gb(used)}"
        else:
            pct = used / t["total"] * 100
            usage = f"{bar(pct)} {fa(round(pct))}٪\n   {fmt_gb(used)} از {fmt_gb(t['total'])}"
        if t["expiryTime"] == 0:
            exp = "♾ نامحدود"
        else:
            left = (t["expiryTime"] / 1000 - time.time()) / 86400
            exp = "⛔️ منقضی شده" if left <= 0 else f"{fa(int(left) + 1)} روز دیگر"
        info = f"📡 وضعیت: {status}\n📊 مصرف: {usage}\n⏳ زمان باقی‌مانده: {exp}"
    else:
        info = "⚠️ اطلاعات مصرف الان در دسترس نیست."
 
    text = (
        f"📦 <b>{svc['plan_name']}</b>\n"
        f"{LINE}\n"
        f"{info}\n"
        f"{LINE}\n"
        "🔗 <b>لینک اشتراک</b> (برای کپی روش بزن):\n"
        f"<code>{SUB_URL}/{svc['sub_id']}</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 تمدید سرویس", callback_data=f"renew:{sid}")
    kb.button(text="🔃 بروزرسانی", callback_data=f"svc:{sid}")
    kb.button(text="🔙 بازگشت", callback_data="my")
    kb.adjust(1, 2)
    await show(cb, text, kb.as_markup())
    await cb.answer()
 
 
@router.callback_query(F.data.startswith("renew:"))
async def renew(cb: CallbackQuery):
    sid = cb.data.split(":")[1]
    kb = InlineKeyboardBuilder()
    for i, p in enumerate(PLANS):
        kb.button(text=f"{NUM_EMOJI[i]} {p['name']} • {fmt_price(p['price'])}", callback_data=f"rp:{sid}:{i}")
    kb.button(text="🔙 بازگشت", callback_data=f"svc:{sid}")
    kb.adjust(1)
    await show(cb, plans_text("🔄 تمدید سرویس") + "\n<i>با تمدید، حجم مصرف‌شده ریست میشه.</i>", kb.as_markup())
    await cb.answer()
 
 
# ───────────────────────── ادمین ─────────────────────────
@router.callback_query(F.data.startswith("ok:"))
async def approve(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("دسترسی نداری", show_alert=True)
    oid = int(cb.data.split(":")[1])
    changed, _ = await q_run(
        "UPDATE orders SET status='processing' WHERE id=? AND status='pending'", (oid,)
    )
    if not changed:
        return await cb.answer("این سفارش قبلاً رسیدگی شده", show_alert=True)
 
    order = await q_one("SELECT * FROM orders WHERE id=?", (oid,))
    plan = PLANS[order["plan_idx"]]
    try:
        if order["service_id"]:
            svc = await q_one("SELECT * FROM services WHERE id=?", (order["service_id"],))
            await xui.renew_client(svc["uuid"], svc["email"], svc["sub_id"], plan["gb"], plan["days"])
            sub_id = svc["sub_id"]
        else:
            email = f"u{order['user_id']}_{secrets.token_hex(3)}"
            uid, sub_id = await xui.add_client(email, plan["gb"], plan["days"])
            await q_run(
                "INSERT INTO services(user_id,email,uuid,sub_id,plan_name,created) VALUES(?,?,?,?,?,?)",
                (order["user_id"], email, uid, sub_id, plan["name"], int(time.time())),
            )
    except Exception:
        logging.exception("provisioning failed for order %s", oid)
        await q_run("UPDATE orders SET status='pending' WHERE id=?", (oid,))
        return await cb.answer("خطا در ارتباط با پنل 3x-ui. لاگ رو چک کن.", show_alert=True)
 
    await q_run("UPDATE orders SET status='done' WHERE id=?", (oid,))
    try:
        await bot.send_message(
            order["user_id"],
            "🎉 <b>پرداختت تأیید شد!</b>\n"
            f"{LINE}\n"
            f"📦 {plan['name']}\n"
            f"{LINE}\n"
            "🔗 <b>لینک اشتراک</b> (برای کپی روش بزن):\n"
            f"<code>{SUB_URL}/{sub_id}</code>\n"
            f"{LINE}\n"
            "این لینک رو توی برنامه (Hiddify، v2rayNG، Streisand و ...) به‌عنوان Subscription اضافه کن.\n"
            "راهنمای کامل: «📚 آموزش اتصال» 👇",
            reply_markup=main_menu(),
        )
    except Exception:
        logging.exception("failed to message user")
    await cb.message.edit_caption(caption=cb.message.html_text + "\n\n✅ تأیید شد", reply_markup=None)
    await cb.answer("انجام شد")
 
 
@router.callback_query(F.data.startswith("no:"))
async def reject(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id not in ADMIN_IDS:
        return await cb.answer("دسترسی نداری", show_alert=True)
    oid = int(cb.data.split(":")[1])
    changed, _ = await q_run(
        "UPDATE orders SET status='rejected' WHERE id=? AND status='pending'", (oid,)
    )
    if not changed:
        return await cb.answer("این سفارش قبلاً رسیدگی شده", show_alert=True)
    order = await q_one("SELECT * FROM orders WHERE id=?", (oid,))
    try:
        await bot.send_message(
            order["user_id"],
            f"❌ <b>سفارش #{fa(oid)} تأیید نشد</b>\n"
            "رسید شما معتبر شناخته نشد. اگه فکر می‌کنی اشتباهه با پشتیبانی تماس بگیر 💬",
            reply_markup=main_menu(),
        )
    except Exception:
        pass
    await cb.message.edit_caption(caption=cb.message.html_text + "\n\n❌ رد شد", reply_markup=None)
    await cb.answer("رد شد")
 
 
@router.message(Command("stats"), F.from_user.id.in_(ADMIN_IDS))
async def stats(m: Message):
    users = (await q_one("SELECT COUNT(*) c FROM users"))["c"]
    svcs = (await q_one("SELECT COUNT(*) c FROM services"))["c"]
    done = await q_one("SELECT COUNT(*) c, COALESCE(SUM(price),0) s FROM orders WHERE status='done'")
    await m.answer(
        "📊 <b>آمار ربات</b>\n"
        f"{LINE}\n"
        f"👥 کاربران: <b>{fa(users)}</b>\n"
        f"📦 سرویس‌ها: <b>{fa(svcs)}</b>\n"
        f"✅ سفارش‌های موفق: <b>{fa(done['c'])}</b>\n"
        f"💰 فروش کل: <b>{fmt_price(done['s'])}</b>"
    )
 
 
@router.message(Command("broadcast"), F.from_user.id.in_(ADMIN_IDS))
async def broadcast(m: Message, bot: Bot):
    text = m.text.partition(" ")[2].strip()
    if not text:
        return await m.answer("استفاده: /broadcast متن پیام")
    ok = 0
    for u in await q_all("SELECT id FROM users"):
        try:
            await bot.send_message(u["id"], text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"برای {ok} نفر ارسال شد.")
 
 
# ───────────────────────── اجرا ─────────────────────────
async def main():
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)
 
 
if __name__ == "__main__":
    asyncio.run(main())
 
