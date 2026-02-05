import sqlite3, qrcode, io, asyncio, httpx, json, base64, os, uuid, random, string, socket, sys
from aiogram import Bot, Dispatcher, Router, F, types, BaseMiddleware
from aiogram.types import Message, CallbackQuery, BotCommand, Update, FSInputFile
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import CommandStart, CommandObject
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from datetime import datetime, timedelta
from dotenv import load_dotenv
from urllib.parse import urlencode
from aiogram.exceptions import TelegramForbiddenError
from pathlib import Path

# --- Load Environment Variables from executable/script directory ---
try:
    exec_dir = Path(sys.argv[0]).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
except Exception:
    exec_dir = Path.cwd()

env_candidates = [exec_dir / '.env', exec_dir / '.env', exec_dir / 'env']
loaded = False
for p in env_candidates:
    if p.exists():
        load_dotenv(str(p))
        loaded = True
        break
if not loaded:
    load_dotenv()

API_TOKEN = os.getenv('API_TOKEN')
ADMIN_IDS = [int(x) for x in os.getenv('ADMIN_IDS').split(',') if x.strip()] if os.getenv('ADMIN_IDS') else []
CHANNELS = [ch.strip() for ch in os.getenv('CHANNELS').split(',')] if os.getenv('CHANNELS') else []
TXUI_PANEL_URL = os.getenv('TXUI_PANEL_URL')
TXUI_USERNAME = os.getenv('TXUI_USERNAME')
TXUI_PASSWORD = os.getenv('TXUI_PASSWORD')
SERVER_DOMAIN = os.getenv('SERVER_DOMAIN')
TEST_INBOUND_REMARK = os.getenv('TEST_INBOUND_REMARK')
WALLET_TRX = os.getenv('WALLET_TRX')
WALLET_TON = os.getenv('WALLET_TON')
# Force IPv4
os.environ["FORCE_IPV4"] = "1"

# Patch DNS to prefer IPv4
orig_getaddrinfo = socket.getaddrinfo

def getaddrinfo_ipv4(*args, **kwargs):
    try:
        return [ai for ai in orig_getaddrinfo(*args, **kwargs) if ai[0] == socket.AF_INET] or orig_getaddrinfo(*args, **kwargs)
    except Exception:
        return orig_getaddrinfo(*args, **kwargs)

socket.getaddrinfo = getaddrinfo_ipv4

# --- Global Variables & FSM States ---
db_conn = None
MAINTENANCE_MODE = False

# This key must match one of the keys in SUB_PLANS_V2
FREE_REWARD_PLAN_KEY = "reward_plan_example"

class PurchaseFlow(StatesGroup):
    get_custom_name = State()
    get_discount_code = State()
    select_plan = State()
    select_payment_method = State()
    select_crypto = State()
    get_receipt = State()

class BulkCreate(StatesGroup):
    select_plan, get_quantity, get_prefix = State(), State(), State()

class AdminTest(StatesGroup):
    get_charge_amount = State()
    get_fake_purchase_amount = State()

# --- Plan Configuration (EXAMPLE DATA) ---
SUB_PLANS_V2 = {
    "plan_a": {"label": "Plan A (Example) - 20GB", "price": 100000, "days": 30, "limit": 20},
    "plan_b": {"label": "Plan B (Example) - 50GB", "price": 200000, "days": 30, "limit": 50},
    "plan_c": {"label": "Plan C (Example) - 100GB", "price": 350000, "days": 30, "limit": 100},
    # Reward plan for referrals
    "reward_plan_example": {"label": "Reward Plan (Gift)", "price": 0, "days": 30, "limit": 10},
}

SUB_PLANS_WG = {
    "wg_plan_a": {"label": "WireGuard Plan (Example) - 30GB", "price": 120000, "days": 30, "limit": 30},
}

# Helper to find plan by key across both services
def get_plan_by_key(key: str):
    if not key: return None, None
    if key in SUB_PLANS_V2:
        return SUB_PLANS_V2[key], 'v2ray'
    if key in SUB_PLANS_WG:
        return SUB_PLANS_WG[key], 'wireguard'
    return None, None

# --- Bot Initialization & Middleware ---
bot = Bot(token=API_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data: dict):
        user = data.get('event_from_user')
        if not MAINTENANCE_MODE or (user and user.id in ADMIN_IDS):
            return await handler(event, data)
        if isinstance(event, Message):
            await event.answer("🔧 ربات در حال حاضر در دست تعمیر است.")
        elif isinstance(event, CallbackQuery):
            await event.answer("🔧 ربات در حال حاضر در دست تعمیر است.", show_alert=True)
        return

# --- Database & Helper Functions ---
def create_db():
    global db_conn
 
    db_conn = sqlite3.connect("example.db", check_same_thread=False)
    db_conn.row_factory = sqlite3.Row
    c = db_conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, plan_key TEXT, service_type TEXT DEFAULT 'v2ray',
        remarks TEXT, txid TEXT, config TEXT, expire_date TEXT, has_test INTEGER DEFAULT 0,
        purchase_count INTEGER DEFAULT 0, referrer_id INTEGER, wallet_balance REAL DEFAULT 0.0,
        successful_referrals INTEGER DEFAULT 0
    )""")

    c.execute("""CREATE TABLE IF NOT EXISTS discounts (
        code TEXT PRIMARY KEY, user_id INTEGER, discount_percentage INTEGER, is_used INTEGER DEFAULT 0
    )""")
    db_conn.commit()

async def log_to_admins(text: str):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, f"🛠 **لاگ سیستم:**\n\n<pre>{text}</pre>", parse_mode=ParseMode.HTML)
        except Exception as e:
            print(f"CRITICAL: Could not send log to admin {admin_id}. Error: {e}")

async def check_subscription(user_id):
    if not CHANNELS or not any(CHANNELS): return True
    for ch in CHANNELS:
        if not ch: continue
        try:
            member = await bot.get_chat_member(chat_id=f"@{ch}", user_id=user_id)
            if member.status not in ("member", "creator", "administrator"): return False
        except Exception:
            return False
    return True

async def get_crypto_price_in_irt(symbol='USDT'):
    try:
        api_symbol = symbol.lower()
        async with httpx.AsyncClient(timeout=10) as client:
            params = {"srcCurrency": api_symbol, "dstCurrency": "rls"}
            response = await client.get("https://apiv2.nobitex.ir/market/stats", params=params)
            response.raise_for_status()
            data = response.json()
            market_key = f"{api_symbol}-rls"
            if market_key not in data.get('stats', {}):
                await log_to_admins(f"Market key '{market_key}' not found in Nobitex response.")
                return None
            latest_price_rials = float(data['stats'][market_key]['latest'])
            return latest_price_rials / 10
    except Exception as e:
        await log_to_admins(f"Error fetching {symbol} price: {e}")
        return None

# --- TXUI Panel Manager ---
class TxuiManager:
    _token = None
    _token_expiry = None

    async def get_token(self):
        if self._token and self._token_expiry and datetime.now() < self._token_expiry:
            return self._token

        try:
            async with httpx.AsyncClient(verify=False, timeout=20.0) as client:
                data = {
                    "username": TXUI_USERNAME,
                    "password": TXUI_PASSWORD
                }

                url = f"{TXUI_PANEL_URL}/login"

                print(f"🔹 در حال ارسال درخواست لاگین به: {url}")
                res = await client.post(url, data=data, follow_redirects=True)
                print(f"🔹 وضعیت پاسخ: {res.status_code}")

                token = res.cookies.get("3x-ui")
                print(f"🔹 کوکی دریافتی 3x-ui: {token}")

                if token:
                    self._token = token
                    self._token_expiry = datetime.now() + timedelta(hours=1)
                    return token
                else:
                    await log_to_admins(f"⚠️ لاگین انجام شد اما توکن خالی است! پاسخ: {res.text[:300]}")

        except Exception as e:
            await log_to_admins(f"❌ خطای دریافت توکن TXUI: {e}")
            return None
txui_manager = TxuiManager()

# --- Main & Menu Handlers ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()
    cur = db_conn.cursor()

    referrer_id = None
    if command and command.args and command.args.startswith("ref_"):
        try:
            ref_id = int(command.args.replace("ref_", ""))
            if ref_id != message.from_user.id: referrer_id = ref_id
        except (ValueError, TypeError): pass

    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (message.from_user.id,))
    if not cur.fetchone():
        cur.execute("INSERT INTO users (user_id, username, referrer_id) VALUES (?, ?, ?)", 
                    (message.from_user.id, message.from_user.username, referrer_id))
        if referrer_id:
            try: await bot.send_message(referrer_id, f"🎉 یک کاربر جدید از طریق لینک شما به ربات پیوست!")
            except Exception: pass
    else:
        cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (message.from_user.username, message.from_user.id))
        
    db_conn.commit()

    try:
        if not await check_subscription(message.from_user.id):
            kb = InlineKeyboardBuilder()
            for ch in CHANNELS:
                if ch: kb.row(types.InlineKeyboardButton(text=f"عضویت در @{ch}", url=f"https://t.me/{ch}"))
            kb.row(types.InlineKeyboardButton(text="✅ عضو شدم", callback_data="check_subs"))
            await message.answer("📰 لطفاً ابتدا در کانال(های) زیر عضو شوید و سپس دکمه 'عضو شدم' را بزنید:", reply_markup=kb.as_markup())
        else:
            await show_main_menu(message)
    except Exception as e:
        await log_to_admins(f"خطا در تابع cmd_start برای کاربر {message.from_user.id}: {e}")

async def show_main_menu(update_obj):
    user_id = update_obj.from_user.id
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🎁 اشتراک تست", callback_data="free_test"), types.InlineKeyboardButton(text="🛒 خرید اشتراک", callback_data="buy_menu"))
    kb.row(types.InlineKeyboardButton(text="♻️ تمدید اشتراک", callback_data="renew_menu"), types.InlineKeyboardButton(text="📈 تعرفه‌ها", callback_data="tariffs"))
    kb.row(types.InlineKeyboardButton(text="🎁 اعتبار رایگان", callback_data="referral_menu"), types.InlineKeyboardButton(text="💼 کیف پول", callback_data="wallet_menu"))
    kb.row(types.InlineKeyboardButton(text="📱 آموزش اتصال", callback_data="guide_menu"), types.InlineKeyboardButton(text="👨‍💼 پشتیبانی", url="https://t.me/NukeNetSuport"))
    if user_id in ADMIN_IDS:
        kb.row(types.InlineKeyboardButton(text="👨‍💻 پنل ادمین", callback_data="admin_panel"))
    text = "سلام به ربات نوک نت خوش آمدید👋\n\n<b>با استفاده از دکمه های زیر خدمات مورد نظر را انتخاب کنید👇</b>"
    target_message = update_obj.message if isinstance(update_obj, CallbackQuery) else update_obj
    try:
        if isinstance(update_obj, CallbackQuery): await update_obj.message.edit_text(text, reply_markup=kb.as_markup())
        else: await target_message.answer(text, reply_markup=kb.as_markup())
    except Exception:
        try: await target_message.answer(text, reply_markup=kb.as_markup())
        except Exception: pass

@router.callback_query(F.data == "check_subs")
async def confirm_subs(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id): await show_main_menu(callback)
    else: await callback.answer("❌ هنوز در تمام کانال‌ها عضو نشده‌اید.", show_alert=True)

@router.callback_query(F.data == "main_menu")
async def back_to_main(callback: CallbackQuery, state: FSMContext): 
    await state.clear()
    await show_main_menu(callback)

@router.callback_query(F.data == "tariffs")
async def show_tariffs(callback: CallbackQuery):
    tariffs_text = "📋 **تعرفه‌های سرویس‌ها:**\n\n"
    for _, plan in SUB_PLANS_V2.items():
        tariffs_text += f"▫️ {plan['label']}\n"
    for _, plan in SUB_PLANS_WG.items():
        tariffs_text += f"▫️ {plan['label']}\n"
    kb = InlineKeyboardBuilder().row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu"))
    await callback.message.edit_text(tariffs_text, reply_markup=kb.as_markup(), parse_mode=ParseMode.MARKDOWN)

@router.callback_query(F.data == "wallet_menu")
async def show_wallet_menu(callback: CallbackQuery):
    cur = db_conn.cursor()
    cur.execute("SELECT wallet_balance FROM users WHERE user_id = ?", (callback.from_user.id,))
    user_db = cur.fetchone()
    balance = user_db['wallet_balance'] if user_db else 0.0

    text = (
        f"💰 **کیف پول شما**\n\n"
        f"موجودی فعلی شما: **{balance:,.0f} تومان**\n\n"
        "شما می‌توانید با دعوت از دوستانتان و یا در آینده با شارژ مستقیم، موجودی خود را افزایش دهید و از آن برای خرید یا تمدید اشتراک استفاده کنید."
    )
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu"))
    await callback.message.edit_text(text, reply_markup=kb.as_markup())

@router.callback_query(F.data == "referral_menu")
async def show_free_credit_menu(callback: CallbackQuery):
    cur = db_conn.cursor()
    cur.execute("SELECT successful_referrals, wallet_balance FROM users WHERE user_id = ?", (callback.from_user.id,))
    user_db = cur.fetchone()
    successful_referrals = user_db['successful_referrals'] if user_db else 0
    balance = user_db['wallet_balance'] if user_db else 0.0
    
    bot_info = await bot.get_me()
    referral_link = f"https://t.me/{bot_info.username}?start=ref_{callback.from_user.id}"

    caption = (
         f"**یه خبر خوب برای اینترنت آزاد! 🚀**\n\n"
        f"با سرویس‌های **NukeNet** می‌تونی بدون محدودیت و با پینگ پایین به اینترنت جهانی وصل بشی. عالی برای وب‌گردی، استریم و مخصوصاً گیم! 🎮\n\n"
        f"**از لینک زیر وارد شو و اولین سرویس تست رایگانت رو بگیر:**\n"
        f"`{referral_link}`\n\n"
        f"---\n\n"
        f"**چطوری سرویست رو رایگان کنی؟** 🤔\n"
        f"این پیام رو برای دوستات بفرست! هر دوستی که از طریق لینک تو وارد بشه و خرید کنه، **۱۰٪ از مبلغ خریدش** به عنوان اعتبار به کیف پولت اضافه می‌شه. به همین راحتی می‌تونی سرویست رو برای همیشه رایگان تمدید کنی! 🔥"
    )
    
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu"))
    
    try:
        await callback.message.delete()
        await callback.message.answer_photo(
            photo=FSInputFile('banner.png'),
            caption=caption,
            reply_markup=kb.as_markup(),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        try:
            await callback.message.edit_caption(caption=caption, reply_markup=kb.as_markup(), parse_mode=ParseMode.MARKDOWN)
        except Exception:
            await callback.message.answer(caption, reply_markup=kb.as_markup(), parse_mode=ParseMode.MARKDOWN)
        await log_to_admins(f"Error sending photo banner for user {callback.from_user.id}: {e}")

# --- Guide Handlers ---
@router.callback_query(F.data == "guide_menu")
async def show_guide_menu(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🤖 Android", callback_data="guide_android"), types.InlineKeyboardButton(text="🍎 iOS", callback_data="guide_ios"))
    kb.row(types.InlineKeyboardButton(text="💻 Windows", callback_data="guide_windows"), types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu"))
    await callback.message.edit_text("📱 لطفا سیستم عامل خود را برای دریافت راهنمای اتصال انتخاب کنید:", reply_markup=kb.as_markup())

@router.callback_query(F.data == "guide_android")
async def guide_android(callback: CallbackQuery):
    url = "https://play.google.com/store/apps/details?id=com.v2ray.ang"
    kb = InlineKeyboardBuilder().row(types.InlineKeyboardButton(text="دانلود V2RayNG", url=url)).row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="guide_menu"))
    await callback.message.edit_text("📲 برای اتصال در اندروید، برنامه V2RayNG را نصب کنید:", reply_markup=kb.as_markup())

@router.callback_query(F.data == "guide_ios")
async def guide_ios(callback: CallbackQuery):
    url = "https://apps.apple.com/us/app/foxray/id6448898396"
    kb = InlineKeyboardBuilder().row(types.InlineKeyboardButton(text="دانلود FoXray", url=url)).row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="guide_menu"))
    await callback.message.edit_text("📲 برای اتصال در آیفون، برنامه FoXray را نصب کنید:", reply_markup=kb.as_markup())

@router.callback_query(F.data == "guide_windows")
async def guide_windows(callback: CallbackQuery):
    url = "https://github.com/2dust/v2rayN/releases/latest"
    kb = InlineKeyboardBuilder().row(types.InlineKeyboardButton(text="دانلود v2rayN", url=url)).row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="guide_menu"))
    await callback.message.edit_text("📲 برای اتصال در ویندوز، برنامه v2rayN را دانلود کنید:", reply_markup=kb.as_markup())


@router.callback_query(F.data.in_("{buy_menu,renew_menu}"))
async def purchase_or_renew_start_generic(callback: CallbackQuery, state: FSMContext):
  
    return await purchase_or_renew_start(callback, state)

@router.callback_query(F.data.in_({"buy_menu", "renew_menu"}))
async def purchase_or_renew_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    is_renewal = callback.data == "renew_menu"
    if is_renewal:
       
        cur = db_conn.cursor()
        cur.execute("SELECT remarks, service_type FROM users WHERE user_id = ? AND remarks IS NOT NULL", (callback.from_user.id,))
        user_data = cur.fetchone()
        if not user_data:
            return await callback.answer("❌ شما هیچ اشتراک فعالی برای تمدید ندارید.", show_alert=True)
        await state.update_data(is_renewal=True, custom_name=user_data['remarks'], service_type=user_data.get('service_type', 'v2ray'))
        await purchase_get_discount(callback, state)
        return

   
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="سرویس V2Ray", callback_data="buy_v2ray"))
    kb.row(types.InlineKeyboardButton(text="سرویس WireGuard", callback_data="buy_wireguard"))
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu"))
    await callback.message.edit_text("🛒 لطفاً نوع سرویسی که می‌خواهید خرید کنید را انتخاب کنید:", reply_markup=kb.as_markup())

@router.callback_query(F.data.in_({"buy_v2ray", "buy_wireguard"}))
async def purchase_choose_service(callback: CallbackQuery, state: FSMContext):
    service = 'v2ray' if callback.data == 'buy_v2ray' else 'wireguard'
    await state.update_data(is_renewal=False, service_type=service)
    await callback.message.edit_text(
        "📝 لطفاً یک نام دلخواه برای اشتراک خود وارد کنید.\n\n(نام باید **انگلیسی**، **بدون فاصله** و **حداقل ۴ کاراکتر** باشد)"
    )
    await state.set_state(PurchaseFlow.get_custom_name)

@router.message(PurchaseFlow.get_custom_name)
async def purchase_get_name(message: Message, state: FSMContext):
    custom_name = message.text.strip()
    if not (custom_name.isalnum() and len(custom_name) >= 4):
        return await message.answer("❌ نام وارد شده نامعتبر است.")
    await state.update_data(custom_name=custom_name)
    await purchase_get_discount(message, state)

async def purchase_get_discount(update: types.Update, state: FSMContext):
    text = "🎁 آیا کد تخفیف دارید؟ لطفاً کد را وارد کنید."
    kb = InlineKeyboardBuilder().row(types.InlineKeyboardButton(text="ادامه بدون کد تخفیف", callback_data="skip_discount"))
    target_message = update.message if isinstance(update, CallbackQuery) else update
    await target_message.answer(text, reply_markup=kb.as_markup())
    await state.set_state(PurchaseFlow.get_discount_code)

@router.message(PurchaseFlow.get_discount_code)
async def purchase_process_discount_code(message: Message, state: FSMContext):
    code = message.text.strip()
    cur = db_conn.cursor()
    cur.execute("SELECT discount_percentage FROM discounts WHERE code = ? AND user_id = ? AND is_used = 0", (code, message.from_user.id))
    discount_data = cur.fetchone()
    
    if discount_data:
        discount = discount_data['discount_percentage']
        await state.update_data(discount_applied=discount, used_code=code)
        await message.answer(f"✅ کد تخفیف {discount}% شما با موفقیت اعمال شد!")
    else:
        await message.answer("❌ کد تخفیف نامعتبر است یا قبلاً استفاده شده. فرآیند بدون تخفیف ادامه می‌یابد.")
        await state.update_data(discount_applied=0)
    
    await purchase_show_plans(message, state)

@router.callback_query(F.data == "skip_discount", PurchaseFlow.get_discount_code)
async def purchase_skip_discount(callback: CallbackQuery, state: FSMContext):
    await state.update_data(discount_applied=0)
    await purchase_show_plans(callback, state)

async def purchase_show_plans(update: types.Update, state: FSMContext):
    user_data = await state.get_data()
    custom_name = user_data.get('custom_name')
    action = "تمدید" if user_data.get('is_renewal') else "خرید"
    service = user_data.get('service_type', 'v2ray')
    kb = InlineKeyboardBuilder()
    plans = SUB_PLANS_V2 if service == 'v2ray' else SUB_PLANS_WG
    for key, plan in plans.items():
        kb.row(types.InlineKeyboardButton(text=plan['label'], callback_data=f"purchase_plan_{key}"))
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu"))
    text = f"نام اشتراک: **{custom_name}**\n\n🛍️ لطفاً پلن مورد نظر برای **{action}** را انتخاب کنید (سرویس: {service}):"
    target_message = update.message if isinstance(update, CallbackQuery) else update
    await target_message.answer(text, reply_markup=kb.as_markup())
    await state.set_state(PurchaseFlow.select_plan)

@router.callback_query(F.data.startswith("purchase_plan_"), PurchaseFlow.select_plan)
async def purchase_select_plan(callback: CallbackQuery, state: FSMContext):
    plan_key = callback.data.replace("purchase_plan_", "")
    plan, service = get_plan_by_key(plan_key)
    if not plan:
        return await callback.answer("❌ پلن نامعتبر است.", show_alert=True)

    await state.update_data(plan_key=plan_key)
    cur = db_conn.cursor()
    cur.execute("SELECT wallet_balance FROM users WHERE user_id = ?", (callback.from_user.id,))
    user_db_data = cur.fetchone()
    balance = user_db_data['wallet_balance'] if user_db_data else 0

    kb = InlineKeyboardBuilder()
    if balance >= plan['price']:
        kb.row(types.InlineKeyboardButton(text=f"💳 پرداخت از کیف پول ({balance:,.0f} تومان)", callback_data="pay_from_wallet"))
    kb.row(types.InlineKeyboardButton(text="💎 پرداخت با ارز دیجیتال", callback_data="pay_crypto"))
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu"))
    await callback.message.edit_text(f"شما پلن **{plan['label']}** را انتخاب کردید.\n\nلطفاً روش پرداخت خود را انتخاب کنید:", reply_markup=kb.as_markup())
    await state.set_state(PurchaseFlow.select_payment_method)

@router.callback_query(F.data == "pay_from_wallet", PurchaseFlow.select_payment_method)
async def pay_from_wallet(callback: CallbackQuery, state: FSMContext):
    user_data = await state.get_data()
    plan_key = user_data['plan_key']
    plan, service = get_plan_by_key(plan_key)
    custom_name = user_data['custom_name']
    is_renewal = user_data.get('is_renewal', False)

    cur = db_conn.cursor()
    cur.execute("UPDATE users SET wallet_balance = wallet_balance - ? WHERE user_id = ?", (plan['price'], callback.from_user.id))
    db_conn.commit()

    if is_renewal:
        await renew_service_for_user(callback, plan, service)
    else:
        await create_service_for_user(callback, plan, custom_name, is_test=False, service=service)
    
    await state.clear()

@router.callback_query(F.data == "pay_crypto", PurchaseFlow.select_payment_method)
async def select_crypto_for_payment(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="💎 پرداخت با ترون (TRX)", callback_data="crypto_type_TRX"))
    kb.row(types.InlineKeyboardButton(text="💎 پرداخت با تون (TON)", callback_data="crypto_type_TON"))
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu"))
    await callback.message.edit_text("لطفاً ارز مورد نظر برای پرداخت را انتخاب کنید:", reply_markup=kb.as_markup())
    await state.set_state(PurchaseFlow.select_crypto)

@router.callback_query(F.data.startswith("crypto_type_"), PurchaseFlow.select_crypto)
async def crypto_payment_start(callback: CallbackQuery, state: FSMContext):
    crypto_symbol = callback.data.replace("crypto_type_", "")
    wallet_map = {"TRX": WALLET_TRX, "TON": WALLET_TON}
    network_map = {"TRX": "TRON", "TON": "TON"}

    wallet_address = wallet_map.get(crypto_symbol)
    network = network_map.get(crypto_symbol)

    if not wallet_address:
        await callback.message.edit_text(f"❌ آدرس کیف پول {crypto_symbol} در سرور تنظیم نشده است.")
        return await state.clear()

    user_data = await state.get_data()
    plan, service = get_plan_by_key(user_data['plan_key'])

    await callback.answer(f"⏳ در حال دریافت قیمت لحظه‌ای {crypto_symbol}...")
    crypto_price_irt = await get_crypto_price_in_irt(crypto_symbol)
    if not crypto_price_irt:
        await callback.message.edit_text("❌ امکان دریافت قیمت لحظه‌ای وجود ندارد. لطفاً دقایقی دیگر مجدد تلاش کنید.")
        return await state.clear()

    required_crypto_amount = round(plan['price'] / crypto_price_irt, 6)
    invoice_id = str(uuid.uuid4().hex[:8]).upper()

    await state.update_data(invoice_id=invoice_id, crypto_amount=required_crypto_amount, crypto_symbol=crypto_symbol)
    payment_params = {
        'amount': required_crypto_amount,
        'coin': crypto_symbol,
        'network': network,
        'address': wallet_address,
        'memo': '' 
    }
    payment_link = f"https://swapwallet.app/express-withdraw?{urlencode(payment_params)}"

    text = (
        f"🧾 **فاکتور شما: `#{invoice_id}`**\n\n"
        f"▫️ **سرویس:** {plan['label']}\n"
        f"▫️ **مبلغ:** `{required_crypto_amount}` **{crypto_symbol}**\n\n"
        "✅ برای پرداخت، روی دکمه زیر کلیک کنید. تمام اطلاعات به صورت خودکار در صفحه پرداخت برای شما پر خواهد شد.\n\n"
        "❗️**مهم:** پس از تکمیل پرداخت، **کد تراکنش (TxID)** را کپی کرده و در همین صفحه برای ربات ارسال کنید."
    )
    if crypto_symbol == 'TON':
        text += "\n\n‼️ **توجه: هنگام پرداخت با تون، فیلد ممو (Memo / Comment) را حتماً خالی بگذارید.**"

    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🔗 پرداخت آنلاین (SwapWallet)", url=payment_link))
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="main_menu"))

    await callback.message.edit_text(text, reply_markup=kb.as_markup(), parse_mode=ParseMode.MARKDOWN)
    await state.set_state(PurchaseFlow.get_receipt)

@router.message(PurchaseFlow.get_receipt)
async def process_receipt(message: Message, state: FSMContext):
    txid = message.text.strip()
    if not (len(txid) >= 60 and len(txid) <= 100 and txid.isalnum()):
        await message.answer("❌ فرمت هش تراکنش (TxID) نامعتبر است. لطفاً هش تراکنش صحیح را وارد کنید.")
        return

    user_data = await state.get_data()
    user_id = message.from_user.id
    plan_key, custom_name = user_data['plan_key'], user_data['custom_name']
    invoice_id, crypto_amount, crypto_symbol = user_data['invoice_id'], user_data['crypto_amount'], user_data['crypto_symbol']
    is_renewal = user_data.get('is_renewal', False)
    plan, service = get_plan_by_key(plan_key)

    admin_text = (
        f"🧾 **رسید جدید برای تایید**\n\n"
        f"1️⃣ **شماره فاکتور:** `{invoice_id}`\n"
        f"2️⃣ **رسید واریز (TxID):**\n<pre>{txid}</pre>\n"
        f"3️⃣ **نوع سرویس:** {plan['label']}\n"
        f"4️⃣ **ارز و میزان پرداختی:** {crypto_amount} {crypto_symbol}\n"
        f"🔄 **نوع عملیات:** {'تمدید' if is_renewal else 'خرید جدید'}\n"
        f"👤 **کاربر:** <a href='tg://user?id={user_id}'>{user_id}</a> ({custom_name})\n\n"
        f"لطفاً تراکنش را بررسی و نتیجه را اعلام کنید."
    )
    
    kb = InlineKeyboardBuilder()
    approve_data = f"approve_{user_id}_{plan_key}_{custom_name}_{1 if is_renewal else 0}"
    reject_data = f"reject_{user_id}"
    kb.row(types.InlineKeyboardButton(text="✅ تایید", callback_data=approve_data), types.InlineKeyboardButton(text="❌ رد", callback_data=reject_data))
    
    for admin_id in ADMIN_IDS:
        try: await bot.send_message(admin_id, admin_text, reply_markup=kb.as_markup())
        except Exception: pass
        
    await message.answer("✅ رسید شما برای بررسی توسط ادمین ارسال شد. لطفاً منتظر بمانید...")
    await state.clear()

@router.callback_query(F.data.startswith("approve_"))
async def approve_payment(callback: CallbackQuery):
    try:
        _, user_id_str, plan_key, custom_name, is_renewal_str = callback.data.split("_", 4)
        user_id, is_renewal = int(user_id_str), bool(int(is_renewal_str))
        plan, service = get_plan_by_key(plan_key)
    except ValueError:
        return await callback.answer("❌ اطلاعات دکمه نامعتبر است.", show_alert=True)

    fake_callback_message = await bot.send_message(user_id, "⏳ پرداخت شما تایید شد، در حال پردازش...")
    fake_callback = types.CallbackQuery(id="fake", from_user=types.User(id=user_id, is_bot=False, first_name=""), chat_instance="", message=fake_callback_message)
    
    if is_renewal:
        await renew_service_for_user(fake_callback, plan, service)
    else:
        await create_service_for_user(fake_callback, plan, custom_name, is_test=False, service=service)
    
    await callback.edit_message_text(f"{callback.message.html_text}\n\n<b>---\n✅ این رسید توسط شما در تاریخ {datetime.now().strftime('%Y-%m-%d %H:%M')} تایید شد.</b>")
    await callback.answer("✅ عملیات برای کاربر انجام شد.")

@router.callback_query(F.data.startswith("reject_"))
async def reject_payment(callback: CallbackQuery):
    _, user_id_str = callback.data.split("_")
    user_id = int(user_id_str)
    await bot.send_message(user_id, "❌ متاسفانه پرداخت شما توسط ادمین تایید نشد. لطفاً با پشتیبانی در تماس باشید.")
    await callback.edit_message_text(f"{callback.message.html_text}\n\n<b>---\n❌ پرداخت توسط شما رد شد.</b>")
    await callback.answer("❌ پیام عدم تایید برای کاربر ارسال شد.")

@router.callback_query(F.data == "free_test")
async def handle_free_test(callback: CallbackQuery):
    cur = db_conn.cursor()
    cur.execute("SELECT has_test FROM users WHERE user_id = ? AND has_test = 1", (callback.from_user.id,))
    if cur.fetchone(): return await callback.answer("⛔️ شما قبلاً اشتراک تست دریافت کرده‌اید.", show_alert=True)
    # Ask user which test type they want
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="🧪 تست V2Ray (1 روز)", callback_data="test_v2"))
    kb.row(types.InlineKeyboardButton(text="🧪 تست WireGuard (1 روز)", callback_data="test_wg"))
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت", callback_data="main_menu"))
    await callback.message.edit_text("کدام سرویس تست را می‌خواهید؟", reply_markup=kb.as_markup())

@router.callback_query(F.data.in_({"test_v2", "test_wg"}))
async def handle_test_choice(callback: CallbackQuery):
    is_wg = callback.data == 'test_wg'
    test_plan = {"days": 1, "limit": 0.5, "label": "تست"}
    service = 'wireguard' if is_wg else 'v2ray'
    await create_service_for_user(callback, test_plan, custom_name=f"test_{callback.from_user.id}", is_test=True, service=service)

async def create_service_for_user(callback: CallbackQuery, plan: dict, custom_name: str, is_test: bool = False, service: str = 'v2ray'):
    user_id = callback.from_user.id
    if is_test: await callback.answer("⏳ در حال ساخت اشتراک تست...", show_alert=False)
    else: await callback.message.edit_text("✅ در حال آماده‌سازی سرویس شما...")
    token = await txui_manager.get_token()
    if not token: return await bot.send_message(user_id, "❌ خطا در ارتباط با پنل.")

    async with httpx.AsyncClient(base_url=TXUI_PANEL_URL, verify=False, timeout=40.0) as client:
        cookies = {"3x-ui": token}
        res = None
        try:
            inbounds_res = await client.get("/panel/api/inbounds/list", cookies=cookies)
            # Try to find an inbound matching TEST_INBOUND_REMARK and the requested service type
            inbounds_list = inbounds_res.json().get('obj', [])
            target_inbound = None
            for i in inbounds_list:
                # prefer remark match
                if i.get('remark') == TEST_INBOUND_REMARK:
                    target_inbound = i
                    break
            if not target_inbound:
                # fallback: try to find any inbound whose streamSettings.network contains expected network
                for i in inbounds_list:
                    try:
                        ss = json.loads(i.get('streamSettings') or '{}')
                        network = ss.get('network')
                        if service == 'v2ray' and network in ('tcp','ws','grpc','kcp','h2','http'):
                            target_inbound = i; break
                        if service == 'wireguard' and network == 'wireguard':
                            target_inbound = i; break
                    except Exception:
                        continue
            if not target_inbound:
                return await bot.send_message(user_id, "⛔️ اینباند مناسب یافت نشد در پنل.")

            target_inbound_id = target_inbound['id']
            inbound_detail_res = await client.get(f"/panel/api/inbounds/get/{target_inbound_id}", cookies=cookies)
            inbound_obj = inbound_detail_res.json()['obj']
            inbound_settings = json.loads(inbound_obj['settings'])

            # Determine how to append a new client based on service type
            remark = custom_name
            new_id = str(uuid.uuid4())
            expiry_ms = int((datetime.now() + timedelta(days=plan['days'])).timestamp() * 1000)
            total_gb = int(plan.get('limit', 0) * 1024 * 1024 * 1024)

            # V2Ray compatible clients list
            if 'clients' in inbound_settings:
                current_clients = inbound_settings.get('clients', [])
                new_client = {"id": new_id, "email": remark, "totalGB": total_gb, "expiryTime": expiry_ms, "limitIp": 2, "enable": True}
                current_clients.append(new_client)
                inbound_settings['clients'] = current_clients
                inbound_obj['settings'] = json.dumps(inbound_settings)
                res = await client.post(f"/panel/api/inbounds/update/{target_inbound_id}", cookies=cookies, json=inbound_obj)
                res.raise_for_status()

                server_address, server_port = SERVER_DOMAIN, target_inbound['port']
                stream_settings = json.loads(target_inbound.get('streamSettings') or '{}')
                params = {'type': stream_settings.get('network', 'tcp'), 'security': stream_settings.get('security', 'none')}
                if params['security'] == 'tls': params['sni'] = stream_settings.get('tlsSettings', {}).get('serverName', server_address)
                connection_link = f"vless://{new_id}@{server_address}:{server_port}?{urlencode(params)}#{remark}"

            # WireGuard-like handling: panels often have 'peers' or 'clients' for WG. We'll try 'peers' first
            elif 'peers' in inbound_settings or inbound_obj.get('protocol','').lower() == 'wireguard':
                current_peers = inbound_settings.get('peers', []) if 'peers' in inbound_settings else inbound_settings.get('clients', [])
                # generate simple base64 keys (note: for production you should generate real WG keys)
                priv_raw = os.urandom(32)
                pub_raw = os.urandom(32)
                priv_b64 = base64.b64encode(priv_raw).decode()
                pub_b64 = base64.b64encode(pub_raw).decode()
                new_peer = {"id": new_id, "email": remark, "totalGB": total_gb, "expiryTime": expiry_ms, "enable": True, "privateKey": priv_b64, "publicKey": pub_b64}
                current_peers.append(new_peer)
                # prefer storing in 'peers' if available
                if 'peers' in inbound_settings:
                    inbound_settings['peers'] = current_peers
                else:
                    inbound_settings['clients'] = current_peers
                inbound_obj['settings'] = json.dumps(inbound_settings)
                res = await client.post(f"/panel/api/inbounds/update/{target_inbound_id}", cookies=cookies, json=inbound_obj)
                res.raise_for_status()

                server_address, server_port = SERVER_DOMAIN, target_inbound['port']
                # Build a wireguard config link (may need manual tweaks depending on panel/server setup)
                connection_link = (
                    f"wg://{pub_b64}@{server_address}:{server_port}?preshared_key={base64.b64encode(os.urandom(16)).decode()}#{remark}"
                )

            else:
                return await bot.send_message(user_id, "❌ ساخت اکانت برای این نوع اینباند پشتیبانی نمی‌شود. لطفاً با پشتیبانی تماس بگیرید.")

            # send QR / link to user
            qr_img = qrcode.make(connection_link)
            bio = io.BytesIO()
            qr_img.save(bio, 'PNG')
            bio.seek(0)
            caption_main = "اشتراک تست" if is_test else f"سرویس {plan.get('label','') }"
            caption = f"✅ {caption_main} شما با نام **{remark}** ساخته شد!\n\n`{connection_link}`"
            await bot.send_photo(chat_id=user_id, photo=types.BufferedInputFile(bio.getvalue(), "config_qr.png"), caption=caption, parse_mode=ParseMode.MARKDOWN)

            if not is_test:
                try: await callback.message.delete()
                except Exception: pass
            cur = db_conn.cursor()
            expire_date = (datetime.now() + timedelta(days=plan['days'])).strftime('%Y-%m-%d')
            if is_test:
                cur.execute("UPDATE users SET has_test = 1, config = ?, remarks = ?, expire_date = ?, service_type = ? WHERE user_id = ?", (connection_link, remark, expire_date, service, user_id))
            else:
                cur.execute("UPDATE users SET plan_key = ?, service_type = ?, remarks = ?, config = ?, expire_date = ? WHERE user_id = ?", (plan.get('label'), service, remark, connection_link, expire_date, user_id))
            db_conn.commit()

            if not is_test:
                cur.execute("UPDATE users SET purchase_count = purchase_count + 1 WHERE user_id = ?", (user_id,))
                cur.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,))
                referrer_data = cur.fetchone()
                if referrer_data and referrer_data['referrer_id']:
                    referrer_id = referrer_data['referrer_id']
                    commission = plan['price'] * 0.10
                    cur.execute("UPDATE users SET wallet_balance = wallet_balance + ?, successful_referrals = successful_referrals + 1 WHERE user_id = ?", (commission, referrer_id))
                    await bot.send_message(referrer_id, f"💰 **پاداش زیرمجموعه!**\n\nیک خرید جدید ثبت شد و **{commission:,.0f} تومان** به کیف پول شما اضافه شد.")
                    cur.execute("SELECT successful_referrals FROM users WHERE user_id = ?", (referrer_id,))
                    referrer_stats = cur.fetchone()
                    if referrer_stats and referrer_stats['successful_referrals'] == 10:
                        reward_plan = SUB_PLANS_V2.get(FREE_REWARD_PLAN_KEY)
                        fake_msg = await bot.send_message(referrer_id, "🎁 شما ۱۰ زیرمجموعه فعال دارید! در حال ساخت سرویس هدیه...")
                        fake_cb = types.CallbackQuery(id="fake", from_user=types.User(id=referrer_id, is_bot=False, first_name=""), chat_instance="", message=fake_msg)
                        await create_service_for_user(fake_cb, reward_plan, custom_name=f"reward_{referrer_id}", is_test=False, service='v2ray')
                db_conn.commit()
        except Exception as e:
            await bot.send_message(user_id, "❌ خطای غیرمنتظره در ساخت سرویس. لطفاً به پشتیبانی اطلاع دهید.")
            await log_to_admins(f"خطای ساخت سرویس: {e}\nپاسخ پنل: {res.text if res and hasattr(res, 'text') else 'No response'}")

async def renew_service_for_user(callback: CallbackQuery, plan: dict, service: str = 'v2ray'):
    user_id = callback.from_user.id
    await callback.message.edit_text("✅ پرداخت تایید شد. در حال تمدید سرویس شما...")
    cur = db_conn.cursor()
    cur.execute("SELECT remarks, service_type FROM users WHERE user_id = ?", (user_id,))
    user_db_data = cur.fetchone()
    if not user_db_data or not user_db_data['remarks']:
        return await bot.send_message(user_id, "❌ اطلاعات اشتراک شما در دیتابیس یافت نشد.")
    user_remark = user_db_data['remarks']
    token = await txui_manager.get_token()
    if not token: return await bot.send_message(user_id, "❌ خطا در ارتباط با پنل.")
    async with httpx.AsyncClient(base_url=TXUI_PANEL_URL, verify=False, timeout=40.0) as client:
        cookies = {"3x-ui": token}
        try:
            inbounds_res = await client.get("/panel/api/inbounds/list", cookies=cookies)
            target_inbound = next((i for i in inbounds_res.json().get('obj', []) if i.get('remark') == TEST_INBOUND_REMARK), None)
            if not target_inbound: return await bot.send_message(user_id, "⛔️ اینباند یافت نشد.")
            target_inbound_id = target_inbound['id']
            inbound_detail_res = await client.get(f"/panel/api/inbounds/get/{target_inbound_id}", cookies=cookies)
            inbound_obj = inbound_detail_res.json()['obj']
            inbound_settings = json.loads(inbound_obj['settings'])
            current_clients = inbound_settings.get('clients', [])
            client_to_renew, client_index = None, -1
            for i, client in enumerate(current_clients):
                if client.get('email') == user_remark:
                    client_to_renew, client_index = client, i
                    break
            if not client_to_renew:
                return await bot.send_message(user_id, "❌ کلاینت شما در پنل یافت نشد.")
            new_total_gb = int(plan['limit'] * 1024 * 1024 * 1024)
            current_expiry_ms = client_to_renew.get('expiryTime', 0)
            now_ms = int(datetime.now().timestamp() * 1000)
            start_time_ms = max(current_expiry_ms, now_ms)
            new_expiry_ms = start_time_ms + (plan['days'] * 24 * 60 * 60 * 1000)
            current_clients[client_index]['totalGB'] = new_total_gb
            current_clients[client_index]['expiryTime'] = new_expiry_ms
            current_clients[client_index]['enable'] = True
            inbound_settings['clients'] = current_clients
            inbound_obj['settings'] = json.dumps(inbound_settings)
            await client.post(f"/panel/api/inbounds/update/{target_inbound_id}", cookies=cookies, json=inbound_obj)
            new_expiry_date_str = datetime.fromtimestamp(new_expiry_ms / 1000).strftime('%Y-%m-%d')
            await bot.send_message(user_id, f"✅ اشتراک شما با موفقیت تمدید شد.\n\n▫️ **سرویس:** {plan['label']}\n▫️ **تاریخ انقضای جدید:** {new_expiry_date_str}")
            await callback.message.delete()
            cur.execute("UPDATE users SET plan_key = ?, expire_date = ? WHERE user_id = ?", (plan['label'], new_expiry_date_str, user_id))
            db_conn.commit()
        except Exception as e:
            await bot.send_message(user_id, "❌ خطای غیرمنتظره در تمدید سرویس رخ داد.")
            await log_to_admins(f"خطای تمدید سرویس: {e}")

# --- Admin / Bulk create updated to support both services ---
@router.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="➕ ساخت اشتراک گروهی", callback_data="bulk_create_start"))
    status_text = "روشن" if MAINTENANCE_MODE else "خاموش"
    kb.row(types.InlineKeyboardButton(text=f"حالت تعمیرات ({status_text})", callback_data="toggle_maintenance"))
    kb.row(types.InlineKeyboardButton(text="🧪 شبیه‌ساز تست", callback_data="admin_test_panel"))
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت به منوی اصلی", callback_data="main_menu"))
    await callback.message.edit_text("👨‍💻 به پنل ادمین خوش آمدید:", reply_markup=kb.as_markup())

@router.callback_query(F.data == "toggle_maintenance")
async def toggle_maintenance(callback: CallbackQuery):
    global MAINTENANCE_MODE
    MAINTENANCE_MODE = not MAINTENANCE_MODE
    await callback.answer(f"✅ حالت تعمیرات {'روشن' if MAINTENANCE_MODE else 'خاموش'} شد.")
    await admin_panel(callback)

@router.callback_query(F.data == "bulk_create_start")
async def bulk_create_start(callback: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardBuilder()
    for key, plan in SUB_PLANS_V2.items():
        kb.row(types.InlineKeyboardButton(text=plan['label'], callback_data=f"bulk_plan_{key}"))
    for key, plan in SUB_PLANS_WG.items():
        kb.row(types.InlineKeyboardButton(text=plan['label'], callback_data=f"bulk_plan_{key}"))
    await callback.message.edit_text("۱. پلن مورد نظر برای ساخت گروهی را انتخاب کنید:", reply_markup=kb.as_markup())
    await state.set_state(BulkCreate.select_plan)

@router.callback_query(F.data.startswith("bulk_plan_"), BulkCreate.select_plan)
async def bulk_create_get_plan(callback: CallbackQuery, state: FSMContext):
    await state.update_data(plan_key=callback.data.replace("bulk_plan_", ""))
    await callback.message.edit_text("۲. تعداد اشتراک مورد نظر را به صورت عدد ارسال کنید:")
    await state.set_state(BulkCreate.get_quantity)

@router.message(BulkCreate.get_quantity)
async def bulk_create_get_quantity(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.answer("❌ لطفاً یک عدد صحیح بزرگتر از صفر وارد کنید.")
    await state.update_data(quantity=int(message.text))
    await message.answer("۳. یک پیشوند برای نام اشتراک‌ها وارد کنید (مثلاً: sale_october):")
    await state.set_state(BulkCreate.get_prefix)

@router.message(BulkCreate.get_prefix)
async def bulk_create_process(message: Message, state: FSMContext):
    data = await state.get_data()
    plan_key = data['plan_key']
    plan, service = get_plan_by_key(plan_key)
    quantity, prefix = data['quantity'], message.text
    await message.answer(f"✅ در حال ساخت {quantity} اشتراک از پلن '{plan['label']}'. لطفاً صبر کنید...")
    token = await txui_manager.get_token()
    if not token:
        await message.answer("❌ خطا در ارتباط با پنل.")
        return await state.clear()
    async with httpx.AsyncClient(base_url=TXUI_PANEL_URL, verify=False, timeout=60.0) as client:
        cookies = {"3x-ui": token}
        try:
            inbounds_res = await client.get("/panel/api/inbounds/list", cookies=cookies)
            target_inbound = next((i for i in inbounds_res.json().get('obj', []) if i.get('remark') == TEST_INBOUND_REMARK), None)
            if not target_inbound: return await message.answer("⛔️ اینباند یافت نشد.")
            target_inbound_id = target_inbound['id']
            inbound_detail_res = await client.get(f"/panel/api/inbounds/get/{target_inbound_id}", cookies=cookies)
            inbound_obj = inbound_detail_res.json()['obj']
            inbound_settings = json.loads(inbound_obj['settings'])
            current_clients = inbound_settings.get('clients', [])
            new_clients, generated_links = [], []
            for i in range(quantity):
                remark, new_uuid = f"{prefix}_{uuid.uuid4().hex[:6]}", str(uuid.uuid4())
                new_client_obj = {"id": new_uuid, "email": remark, "totalGB": int(plan['limit'] * 1024 * 1024 * 1024), "expiryTime": int((datetime.now() + timedelta(days=plan['days'])).timestamp() * 1000), "limitIp": 2, "enable": True}
                new_clients.append(new_client_obj)
                server_address, server_port = SERVER_DOMAIN, target_inbound['port']
                stream_settings = json.loads(target_inbound.get('streamSettings') or '{}')
                params = {'type': stream_settings.get('network', 'tcp'), 'security': stream_settings.get('security', 'none')}
                if params['security'] == 'tls': params['sni'] = stream_settings.get('tlsSettings', {}).get('serverName', server_address)
                link = f"vless://{new_uuid}@{server_address}:{server_port}?{urlencode(params)}#{remark}"
                generated_links.append(link)
            current_clients.extend(new_clients)
            inbound_settings['clients'] = current_clients
            inbound_obj['settings'] = json.dumps(inbound_settings)
            await client.post(f"/panel/api/inbounds/update/{target_inbound_id}", cookies=cookies, json=inbound_obj)
            file_content = "\n".join(generated_links)
            file_bio = io.BytesIO(file_content.encode('utf-8'))
            await message.answer_document(types.BufferedInputFile(file_bio.getvalue(), f"{prefix}_configs.txt"), caption=f"✅ {quantity} اشتراک با موفقیت ساخته شد.")
            await log_to_admins(f"ادمین {message.from_user.id} تعداد {quantity} اشتراک از پلن {plan['label']} ساخت.")
        except Exception as e:
            await message.answer("❌ خطای غیرمنتظره در ساخت اشتراک‌ها.")
            await log_to_admins(f"خطای ساخت گروهی: {e}")
    await state.clear()

@router.callback_query(F.data == "admin_test_panel")
async def admin_test_panel(event: types.Union[Message, CallbackQuery]):
    kb = InlineKeyboardBuilder()
    kb.row(types.InlineKeyboardButton(text="💰 شارژ تستی کیف پول", callback_data="charge_wallet_test"))
    kb.row(types.InlineKeyboardButton(text="👥 تست خرید زیرمجموعه", callback_data="referral_purchase_test"))
    kb.row(types.InlineKeyboardButton(text="🔙 بازگشت به پنل ادمین", callback_data="admin_panel"))
    text = "🧪 به بخش شبیه‌ساز تست خوش آمدید. لطفاً عملیات مورد نظر را انتخاب کنید:"
    if isinstance(event, types.CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb.as_markup())
    else:
        await event.answer(text, reply_markup=kb.as_markup())

@router.message(AdminTest.get_charge_amount)
async def process_wallet_charge_test(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.answer("❌ لطفاً یک عدد صحیح و مثبت وارد کنید.")
    amount = int(message.text)
    admin_id = message.from_user.id
    cur = db_conn.cursor()
    cur.execute("UPDATE users SET wallet_balance = wallet_balance + ? WHERE user_id = ?", (amount, admin_id))
    db_conn.commit()
    cur.execute("SELECT wallet_balance FROM users WHERE user_id = ?", (admin_id,))
    new_balance = cur.fetchone()['wallet_balance']
    await message.answer(f"✅ مبلغ **{amount:,.0f} تومان** با موفقیت به کیف پول شما اضافه شد.\n"                         f"موجودی جدید شما: **{new_balance:,.0f} تومان**")
    await state.clear()
    await admin_test_panel(message)

@router.callback_query(F.data == "referral_purchase_test")
async def referral_test_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("👥 لطفاً مبلغ خرید فرضی (به تومان) که توسط زیرمجموعه شما انجام شده را وارد کنید.\nکمیسیون ۱۰٪ از این مبلغ برای شما محاسبه خواهد شد.")
    await state.set_state(AdminTest.get_fake_purchase_amount)

@router.message(AdminTest.get_fake_purchase_amount)
async def process_referral_test(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.answer("❌ لطفاً یک عدد صحیح و مثبت وارد کنید.")
    fake_price = int(message.text)
    admin_id = message.from_user.id
    commission = fake_price * 0.10
    cur = db_conn.cursor()
    cur.execute("UPDATE users SET wallet_balance = wallet_balance + ?, successful_referrals = successful_referrals + 1 WHERE user_id = ?",
                (commission, admin_id))
    db_conn.commit()
    await message.answer(f"✅ تست خرید زیرمجموعه با موفقیت انجام شد.\n▫️ مبلغ **{commission:,.0f} تومان** (۱۰٪ از {fake_price:,.0f}) به کیف پول شما اضافه شد.\n▫️ شمارنده زیرمجموعه‌های موفق شما یک عدد افزایش یافت.")
    cur.execute("SELECT successful_referrals FROM users WHERE user_id = ?", (admin_id,))
    referrer_stats = cur.fetchone()
    if referrer_stats and referrer_stats['successful_referrals'] == 10:
        await message.answer("🎉 **تبریک!** شما به ۱۰ زیرمجموعه موفق رسیدید. در حال ساخت سرویس هدیه برای شما...")
        reward_plan = SUB_PLANS_V2.get(FREE_REWARD_PLAN_KEY)
        fake_msg = await message.answer("درحال ساخت سرویس هدیه تستی...")
        fake_cb = types.CallbackQuery(id="fake_reward_cb", from_user=message.from_user, chat_instance="fake", message=fake_msg)
        await create_service_for_user(fake_cb, reward_plan, custom_name=f"reward_test_{admin_id}", is_test=False, service='v2ray')
    await state.clear()
    await admin_test_panel(message)

@dp.startup()
async def on_startup(bot: Bot):
    create_db()
    with sqlite3.connect("example.db") as conn:
        c = conn.cursor()
        for admin_id in ADMIN_IDS:
            c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (admin_id,))
        conn.commit()
    await bot.set_my_commands([BotCommand(command="start", description="شروع ربات")])

async def main():
    required_vars = [API_TOKEN, ADMIN_IDS, TXUI_PANEL_URL, TXUI_USERNAME, TXUI_PASSWORD, SERVER_DOMAIN, TEST_INBOUND_REMARK, WALLET_TRX, WALLET_TON]
    if not all(required_vars):
        print("!!! خطای مهم: یک یا چند متغیر اصلی در فایل .env تعریف نشده است.")
        return
    dp.update.middleware.register(MaintenanceMiddleware())
    print("Bot started...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())