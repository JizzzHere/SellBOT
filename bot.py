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
    {"name": "۱ ماهه - ۳۰ گیگ", "gb": 30, "days": 30, "price": 150_000},
    {"name": "۱ ماهه - ۶۰ گیگ", "gb": 60, "days": 30, "price": 250_000},
    {"name": "۳ ماهه - ۱۰۰ گیگ", "gb": 100, "days": 90, "price": 400_000},
    {"name": "۳ ماهه - نامحدود", "gb": 0, "days": 90, "price": 600_000},
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
def fmt_price(p: int) -> str:
    return f"{p:,} تومان"


def fmt_gb(b: float) -> str:
    return f"{b / 1024**3:.2f} GB"


def main_menu():
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 خرید سرویس", callback_data="buy")
    kb.button(text="📦 سرویس‌های من", callback_data="my")
    kb.button(text="💬 پشتیبانی", url=f"https://t.me/{SUPPORT_USERNAME}")
    kb.adjust(1)
    return kb.as_markup()


def plans_kb(prefix: str):
    kb = InlineKeyboardBuilder()
    for i, p in enumerate(PLANS):
        kb.button(text=f"{p['name']} — {fmt_price(p['price'])}", callback_data=f"{prefix}:{i}")
    kb.button(text="🔙 بازگشت", callback_data="menu")
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
    await m.answer("سلام 👋\nبه ربات فروش VPN خوش اومدی. یکی از گزینه‌ها رو انتخاب کن:", reply_markup=main_menu())


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "منوی اصلی:", main_menu())
    await cb.answer()


@router.callback_query(F.data == "buy")
async def buy(cb: CallbackQuery):
    await show(cb, "پلن مورد نظرت رو انتخاب کن:", plans_kb("plan"))
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
    text = (
        f"🧾 سفارش #{order_id}\n"
        f"پلن: <b>{plan['name']}</b>\n"
        f"مبلغ: <b>{fmt_price(plan['price'])}</b>\n\n"
        f"لطفاً مبلغ رو به کارت زیر واریز کن:\n"
        f"<code>{CARD_NUMBER}</code>\n"
        f"به نام: {CARD_HOLDER}\n\n"
        f"بعد از پرداخت، <b>عکس رسید</b> رو همینجا بفرست."
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
    kind = "تمدید" if order["service_id"] else "خرید جدید"

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید", callback_data=f"ok:{order_id}")
    kb.button(text="❌ رد", callback_data=f"no:{order_id}")
    caption = (
        f"🧾 سفارش #{order_id} ({kind})\n"
        f'کاربر: <a href="tg://user?id={m.from_user.id}">{html.escape(m.from_user.full_name)}</a> '
        f"(<code>{m.from_user.id}</code>)\n"
        f"پلن: {plan['name']}\nمبلغ: {fmt_price(order['price'])}"
    )
    for admin in ADMIN_IDS:
        try:
            await bot.send_photo(admin, file_id, caption=caption, reply_markup=kb.as_markup())
        except Exception:
            logging.exception("failed to notify admin %s", admin)
    await state.clear()
    await m.answer("✅ رسید ثبت شد. بعد از تأیید ادمین، سرویس برات ارسال میشه.", reply_markup=main_menu())


@router.message(Pay.waiting_receipt)
async def not_photo(m: Message):
    await m.answer("لطفاً فقط <b>عکس رسید</b> رو بفرست (یا /start برای انصراف).")


@router.callback_query(F.data == "my")
async def my_services(cb: CallbackQuery):
    rows = await q_all("SELECT * FROM services WHERE user_id=?", (cb.from_user.id,))
    if not rows:
        kb = InlineKeyboardBuilder()
        kb.button(text="🛒 خرید سرویس", callback_data="buy")
        kb.button(text="🔙 بازگشت", callback_data="menu")
        kb.adjust(1)
        await show(cb, "هنوز سرویسی نداری.", kb.as_markup())
        return await cb.answer()
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"{r['plan_name']} ({r['email']})", callback_data=f"svc:{r['id']}")
    kb.button(text="🔙 بازگشت", callback_data="menu")
    kb.adjust(1)
    await show(cb, "سرویس‌های تو:", kb.as_markup())
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
        total = "نامحدود" if t["total"] == 0 else fmt_gb(t["total"])
        if t["expiryTime"] == 0:
            exp = "نامحدود"
        else:
            left = (t["expiryTime"] / 1000 - time.time()) / 86400
            exp = "منقضی شده" if left <= 0 else f"{int(left)} روز دیگر"
        info = f"مصرف: {fmt_gb(used)} از {total}\nانقضا: {exp}\n"
    else:
        info = "(اطلاعات مصرف در دسترس نیست)\n"
    text = (
        f"📦 <b>{svc['plan_name']}</b>\n{info}\n"
        f"لینک اشتراک:\n<code>{SUB_URL}/{svc['sub_id']}</code>"
    )
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 تمدید", callback_data=f"renew:{sid}")
    kb.button(text="🔙 بازگشت", callback_data="my")
    kb.adjust(1)
    await show(cb, text, kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("renew:"))
async def renew(cb: CallbackQuery):
    sid = cb.data.split(":")[1]
    kb = InlineKeyboardBuilder()
    for i, p in enumerate(PLANS):
        kb.button(text=f"{p['name']} — {fmt_price(p['price'])}", callback_data=f"rp:{sid}:{i}")
    kb.button(text="🔙 بازگشت", callback_data=f"svc:{sid}")
    kb.adjust(1)
    await show(cb, "پلن تمدید رو انتخاب کن (حجم مصرف‌شده ریست میشه):", kb.as_markup())
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
            f"✅ پرداختت تأیید شد!\n\nلینک اشتراک:\n<code>{SUB_URL}/{sub_id}</code>\n\n"
            "این لینک رو توی برنامه (v2rayNG، Hiddify، Streisand و ...) به‌عنوان Subscription اضافه کن.",
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
            f"❌ سفارش #{oid} تأیید نشد. اگه فکر می‌کنی اشتباهه با پشتیبانی تماس بگیر.",
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
        f"👥 کاربران: {users}\n📦 سرویس‌ها: {svcs}\n"
        f"✅ سفارش‌های موفق: {done['c']}\n💰 فروش کل: {fmt_price(done['s'])}"
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
