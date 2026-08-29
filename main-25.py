import os
import re
import html
import base64
import hashlib
import sqlite3
import logging
from datetime import datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot,
    BotCommand,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================================================
# SETTINGS
# =========================================================

FACTORY_BOT_TOKEN = os.getenv("FACTORY_BOT_TOKEN", "").strip()
FACTORY_OWNER_ID = int(os.getenv("FACTORY_OWNER_ID", "0"))
FACTORY_NAME = os.getenv("FACTORY_NAME", "HAEDR MARKET").strip()
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "").strip().replace("@", "")

DB_PATH = os.getenv(
    "DB_PATH",
    "/data/haedr_factory.db" if os.path.isdir("/data") else "haedr_factory.db",
)

_SECRET_SEED = os.getenv("FACTORY_SECRET_KEY", FACTORY_BOT_TOKEN or "change-me")
_FERNET_KEY = base64.urlsafe_b64encode(hashlib.sha256(_SECRET_SEED.encode()).digest())
FERNET = Fernet(_FERNET_KEY)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("haedr-market-factory")


# =========================================================
# DATABASE
# =========================================================

def now_dt():
    return datetime.utcnow()


def now_iso():
    return now_dt().isoformat(timespec="seconds")


def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                banned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS customer_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                telegram_bot_id INTEGER NOT NULL UNIQUE,
                bot_username TEXT NOT NULL,
                bot_name TEXT NOT NULL,
                token_encrypted TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                template_status TEXT NOT NULL DEFAULT 'registered',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(owner_user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                duration_days INTEGER NOT NULL DEFAULT 30,
                bot_limit INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payment_methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                address TEXT DEFAULT '',
                description TEXT DEFAULT '',
                active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                package_id INTEGER NOT NULL,
                method_id INTEGER NOT NULL,
                request_type TEXT NOT NULL DEFAULT 'purchase',
                amount REAL NOT NULL,
                proof_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                admin_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(package_id) REFERENCES packages(id),
                FOREIGN KEY(method_id) REFERENCES payment_methods(id)
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                package_id INTEGER NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(package_id) REFERENCES packages(id)
            );
            """
        )

        defaults = {
            "support_username": SUPPORT_USERNAME,
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
            )

        now = now_iso()
        for method_name in ("شام كاش", "سيريتل كاش"):
            conn.execute(
                """
                INSERT OR IGNORE INTO payment_methods
                (name, address, description, active, created_at, updated_at)
                VALUES (?, '', '', 0, ?, ?)
                """,
                (method_name, now, now),
            )

        count = conn.execute("SELECT COUNT(*) AS c FROM packages").fetchone()["c"]
        if count == 0:
            conn.execute(
                """
                INSERT INTO packages
                (name, price, duration_days, bot_limit, active, created_at, updated_at)
                VALUES ('اشتراك شهري', 5, 30, 1, 1, ?, ?)
                """,
                (now, now),
            )


def ensure_user(user):
    if not user:
        return
    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id, username, first_name, banned, created_at, updated_at)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (user.id, user.username or "", user.first_name or "", now, now),
        )
        conn.execute(
            """
            UPDATE users
            SET username = ?, first_name = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (user.username or "", user.first_name or "", now, user.id),
        )


def is_owner(user_id):
    return user_id == FACTORY_OWNER_ID


def is_banned(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT banned FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return bool(row and row["banned"])


def get_setting(key, default=""):
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )


def encrypt_token(token):
    return FERNET.encrypt(token.encode()).decode()


def decrypt_token(encrypted):
    try:
        return FERNET.decrypt(encrypted.encode()).decode()
    except InvalidToken:
        return None


def user_bots(user_id):
    with db() as conn:
        return conn.execute(
            """
            SELECT * FROM customer_bots
            WHERE owner_user_id = ?
            ORDER BY id DESC
            """,
            (user_id,),
        ).fetchall()


def get_customer_bot(bot_id, owner_user_id=None):
    with db() as conn:
        if owner_user_id is None:
            return conn.execute(
                "SELECT * FROM customer_bots WHERE id = ?",
                (bot_id,),
            ).fetchone()
        return conn.execute(
            """
            SELECT * FROM customer_bots
            WHERE id = ? AND owner_user_id = ?
            """,
            (bot_id, owner_user_id),
        ).fetchone()


def active_subscription(user_id):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT s.*, p.name AS package_name, p.bot_limit, p.duration_days, p.price
            FROM subscriptions s
            JOIN packages p ON p.id = s.package_id
            WHERE s.user_id = ? AND s.active = 1
            ORDER BY s.id DESC
            """,
            (user_id,),
        ).fetchall()

    now = now_dt()
    for row in rows:
        end_at = parse_dt(row["end_at"])
        if end_at and end_at > now:
            return row
    return None


def remaining_bot_slots(user_id):
    sub = active_subscription(user_id)
    if not sub:
        return 0
    current = len(user_bots(user_id))
    return max(0, int(sub["bot_limit"]) - current)


# =========================================================
# UI
# =========================================================

def blue(text, data):
    return InlineKeyboardButton(text=text, callback_data=data, style="primary")


def red(text, data):
    return InlineKeyboardButton(text=text, callback_data=data, style="danger")


def main_menu(user_id):
    sub = active_subscription(user_id)
    slots = remaining_bot_slots(user_id)

    if sub and slots > 0:
        primary = [blue("➕ إنشاء بوت جديد", "create_bot")]
    else:
        primary = [blue("🛒 شراء بوت", "buy_bot")]

    rows = [
        primary,
        [
            blue("🤖 بوتاتي", "my_bots"),
            red("💳 تجديد الاشتراك", "renew"),
        ],
        [
            blue("📦 طلبات الدفع", "my_payments"),
            red("🎧 الدعم الفني", "support"),
        ],
    ]

    if is_owner(user_id):
        rows.append([red("⚙️ إدارة الصانع", "factory_admin")])

    return InlineKeyboardMarkup(rows)


def back_home():
    return InlineKeyboardMarkup([[red("⬅️ الرئيسية", "home")]])


def admin_menu():
    return InlineKeyboardMarkup(
        [
            [
                blue("📊 الإحصائيات", "admin_stats"),
                red("💰 الباقات والأسعار", "admin_packages"),
            ],
            [
                blue("💳 طرق الدفع", "admin_payment_methods"),
                red("📥 طلبات الدفع", "admin_payment_requests"),
            ],
            [
                blue("🤖 بوتات العملاء", "admin_all_bots"),
                red("👥 العملاء", "admin_users"),
            ],
            [
                blue("📅 الاشتراكات", "admin_subscriptions"),
                red("📢 الإذاعة", "admin_broadcast"),
            ],
            [
                blue("🎧 تعديل الدعم", "admin_support"),
            ],
            [red("⬅️ الرئيسية", "home")],
        ]
    )


# =========================================================
# PROFILE / COMMANDS
# =========================================================

async def post_init(application):
    try:
        await application.bot.set_my_description(
            description=f"{FACTORY_NAME} 🤖\nشراء وإنشاء وإدارة بوتات تيليجرام."
        )
        await application.bot.set_my_short_description(
            short_description="شراء وإنشاء وإدارة بوتات تيليجرام"
        )
        await application.bot.set_my_commands(
            [
                BotCommand("start", "🏠 فتح الصانع"),
                BotCommand("mybots", "🤖 بوتاتي"),
                BotCommand("id", "🆔 معرف حسابي"),
                BotCommand("admin", "⚙️ إدارة الصانع"),
            ]
        )
    except TelegramError as exc:
        logger.warning("Could not set profile: %s", exc)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text("⛔ حسابك محظور من استخدام الصانع.")
        return

    context.user_data.clear()
    sub = active_subscription(user.id)

    if sub:
        end_text = parse_dt(sub["end_at"]).strftime("%Y-%m-%d")
        sub_text = f"\n\n✅ اشتراكك فعال حتى: <b>{end_text}</b>"
    else:
        sub_text = "\n\n⚠️ ما عندك اشتراك فعال حالياً."

    await update.message.reply_text(
        f"👋 <b>أهلاً {html.escape(user.first_name or 'صديقي')}</b>\n\n"
        f"🤖 <b>{html.escape(FACTORY_NAME)}</b>\n\n"
        "اشتري بوتك، فعّل اشتراكك، وبعد الموافقة بيصير فيك تنشئ البوت."
        f"{sub_text}\n\n"
        "اختر المطلوب 👇",
        parse_mode="HTML",
        reply_markup=main_menu(user.id),
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.message.reply_text(
        f"🆔 <b>معرف حسابك:</b>\n\n<code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


async def my_bots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await show_my_bots_message(update.message, update.effective_user.id)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("❌ هذا الأمر للمالك فقط.")
        return
    context.user_data.clear()
    await update.message.reply_text(
        "⚙️ <b>إدارة الصانع</b>",
        parse_mode="HTML",
        reply_markup=admin_menu(),
    )


# =========================================================
# CUSTOMER FLOW
# =========================================================

async def show_packages(query, request_type):
    with db() as conn:
        packages = conn.execute(
            "SELECT * FROM packages WHERE active = 1 ORDER BY price, id"
        ).fetchall()

    rows = []
    for p in packages:
        rows.append([
            blue(
                f"🤖 {p['name']} — {p['price']:.2f}$",
                f"package:{request_type}:{p['id']}",
            )
        ])

    if not rows:
        rows.append([blue("📭 لا توجد باقات متاحة", "noop")])

    rows.append([red("⬅️ الرئيسية", "home")])

    title = "🛒 <b>شراء بوت</b>" if request_type == "purchase" else "💳 <b>تجديد الاشتراك</b>"
    await query.edit_message_text(
        f"{title}\n\nاختر الباقة المناسبة 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_payment_methods(query, request_type, package_id):
    with db() as conn:
        package = conn.execute(
            "SELECT * FROM packages WHERE id = ? AND active = 1",
            (package_id,),
        ).fetchone()
        methods = conn.execute(
            "SELECT * FROM payment_methods WHERE active = 1 ORDER BY id"
        ).fetchall()

    if not package:
        await query.answer("الباقة غير متاحة.", show_alert=True)
        return

    rows = []
    for m in methods:
        rows.append([
            blue(
                f"💳 {m['name']}",
                f"paymethod:{request_type}:{package_id}:{m['id']}",
            )
        ])

    if not rows:
        rows.append([blue("📭 لا توجد طرق دفع مفعلة", "noop")])

    rows.append([red("⬅️ الباقات", f"{'buy_bot' if request_type == 'purchase' else 'renew'}")])

    await query.edit_message_text(
        f"🤖 <b>{html.escape(package['name'])}</b>\n\n"
        f"💵 السعر: <b>{package['price']:.2f}$</b>\n"
        f"📅 المدة: <b>{package['duration_days']} يوم</b>\n"
        f"🤖 عدد البوتات: <b>{package['bot_limit']}</b>\n\n"
        "اختر طريقة الدفع 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def begin_payment(query, context, request_type, package_id, method_id):
    with db() as conn:
        package = conn.execute(
            "SELECT * FROM packages WHERE id = ? AND active = 1",
            (package_id,),
        ).fetchone()
        method = conn.execute(
            "SELECT * FROM payment_methods WHERE id = ? AND active = 1",
            (method_id,),
        ).fetchone()

    if not package or not method:
        await query.answer("الخيار غير متاح.", show_alert=True)
        return

    if not method["address"]:
        await query.answer("طريقة الدفع غير مجهزة بعد.", show_alert=True)
        return

    context.user_data.clear()
    context.user_data["state"] = "waiting_payment_proof"
    context.user_data["request_type"] = request_type
    context.user_data["package_id"] = package_id
    context.user_data["method_id"] = method_id

    description = method["description"] or "حوّل المبلغ ثم أرسل صورة إثبات التحويل."

    await query.edit_message_text(
        f"💳 <b>{html.escape(method['name'])}</b>\n\n"
        f"🤖 الباقة: <b>{html.escape(package['name'])}</b>\n"
        f"💵 المبلغ المطلوب: <b>{package['price']:.2f}$</b>\n\n"
        f"📍 <b>عنوان التحويل:</b>\n"
        f"<code>{html.escape(method['address'])}</code>\n\n"
        f"📝 <b>التعليمات:</b>\n{html.escape(description)}\n\n"
        "📸 بعد التحويل أرسل صورة إثبات الدفع هون.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "home")]]),
    )


async def show_my_payments(query):
    with db() as conn:
        rows_db = conn.execute(
            """
            SELECT pr.*, p.name AS package_name, pm.name AS method_name
            FROM payment_requests pr
            JOIN packages p ON p.id = pr.package_id
            JOIN payment_methods pm ON pm.id = pr.method_id
            WHERE pr.user_id = ?
            ORDER BY pr.id DESC
            LIMIT 20
            """,
            (query.from_user.id,),
        ).fetchall()

    status_map = {
        "pending": "⏳ قيد المراجعة",
        "approved": "✅ مقبول",
        "rejected": "❌ مرفوض",
    }

    if not rows_db:
        text = "📦 <b>طلبات الدفع</b>\n\nما عندك طلبات دفع."
    else:
        parts = ["📦 <b>طلبات الدفع</b>\n"]
        for row in rows_db:
            parts.append(
                f"\n🧾 <b>#{row['id']}</b> — {html.escape(row['package_name'])}\n"
                f"💳 {html.escape(row['method_name'])}\n"
                f"💵 {row['amount']:.2f}$\n"
                f"📌 {status_map.get(row['status'], row['status'])}"
            )
        text = "\n".join(parts)

    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=back_home(),
    )


async def show_my_bots_message(message, user_id):
    bots = user_bots(user_id)
    rows = []

    for row in bots[:30]:
        icon = "✅" if row["active"] else "⛔"
        rows.append([blue(f"{icon} @{row['bot_username']}", f"customer_bot:{row['id']}")])

    if active_subscription(user_id) and remaining_bot_slots(user_id) > 0:
        rows.append([blue("➕ إنشاء بوت جديد", "create_bot")])
    else:
        rows.append([blue("🛒 شراء / تجديد", "buy_bot")])

    rows.append([red("⬅️ الرئيسية", "home")])

    await message.reply_text(
        f"🤖 <b>بوتاتي</b>\n\nعدد البوتات: <b>{len(bots)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_my_bots_query(query):
    bots = user_bots(query.from_user.id)
    rows = []

    for row in bots[:30]:
        icon = "✅" if row["active"] else "⛔"
        rows.append([blue(f"{icon} @{row['bot_username']}", f"customer_bot:{row['id']}")])

    if active_subscription(query.from_user.id) and remaining_bot_slots(query.from_user.id) > 0:
        rows.append([blue("➕ إنشاء بوت جديد", "create_bot")])

    rows.append([red("⬅️ الرئيسية", "home")])

    await query.edit_message_text(
        f"🤖 <b>بوتاتي</b>\n\nعدد البوتات: <b>{len(bots)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_customer_bot(query, bot_record_id):
    row = get_customer_bot(bot_record_id, owner_user_id=query.from_user.id)
    if not row:
        await query.answer("البوت غير موجود.", show_alert=True)
        return

    await query.edit_message_text(
        f"🤖 <b>{html.escape(row['bot_name'])}</b>\n\n"
        f"🔗 @{html.escape(row['bot_username'])}\n"
        f"🆔 <code>{row['telegram_bot_id']}</code>\n"
        f"📌 {'فعال ✅' if row['active'] else 'متوقف ⛔'}\n\n"
        "البوت مسجل عندك بالصانع.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("⏯ تفعيل / إيقاف", f"customer_bot_toggle:{row['id']}")],
                [red("🗑 حذف البوت", f"customer_bot_delete:{row['id']}")],
                [red("⬅️ بوتاتي", "my_bots")],
            ]
        ),
    )


# =========================================================
# ADMIN PAGES
# =========================================================

async def admin_stats(query):
    with db() as conn:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        bots = conn.execute("SELECT COUNT(*) AS c FROM customer_bots").fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM payment_requests WHERE status = 'pending'"
        ).fetchone()["c"]
        subs = conn.execute(
            "SELECT COUNT(*) AS c FROM subscriptions WHERE active = 1"
        ).fetchone()["c"]

    await query.edit_message_text(
        "📊 <b>إحصائيات الصانع</b>\n\n"
        f"👥 المستخدمون: <b>{users}</b>\n"
        f"🤖 البوتات: <b>{bots}</b>\n"
        f"📥 دفعات معلقة: <b>{pending}</b>\n"
        f"📅 الاشتراكات: <b>{subs}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[red("⬅️ إدارة الصانع", "factory_admin")]]),
    )


async def admin_packages(query):
    with db() as conn:
        packages = conn.execute("SELECT * FROM packages ORDER BY id DESC").fetchall()

    rows = [[blue("➕ إضافة باقة", "admin_package_add")]]
    for p in packages:
        rows.append([
            blue(
                f"{'✅' if p['active'] else '⛔'} {p['name']} — {p['price']:.2f}$",
                f"admin_package:{p['id']}",
            )
        ])
    rows.append([red("⬅️ إدارة الصانع", "factory_admin")])

    await query.edit_message_text(
        "💰 <b>الباقات والأسعار</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_package_page(query, package_id):
    with db() as conn:
        p = conn.execute("SELECT * FROM packages WHERE id = ?", (package_id,)).fetchone()

    if not p:
        await query.answer("الباقة غير موجودة.", show_alert=True)
        return

    await query.edit_message_text(
        f"💰 <b>{html.escape(p['name'])}</b>\n\n"
        f"💵 السعر: <b>{p['price']:.2f}$</b>\n"
        f"📅 المدة: <b>{p['duration_days']} يوم</b>\n"
        f"🤖 عدد البوتات: <b>{p['bot_limit']}</b>\n"
        f"📌 {'مفعلة ✅' if p['active'] else 'متوقفة ⛔'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("💵 تعديل السعر", f"admin_package_price:{package_id}")],
                [red("⏯ تفعيل / إيقاف", f"admin_package_toggle:{package_id}")],
                [red("⬅️ الباقات", "admin_packages")],
            ]
        ),
    )


async def admin_payment_methods(query):
    with db() as conn:
        methods = conn.execute("SELECT * FROM payment_methods ORDER BY id").fetchall()

    rows = []
    for m in methods:
        rows.append([
            blue(
                f"{'✅' if m['active'] else '⛔'} {m['name']}",
                f"admin_payment_method:{m['id']}",
            )
        ])
    rows.append([red("⬅️ إدارة الصانع", "factory_admin")])

    await query.edit_message_text(
        "💳 <b>طرق الدفع</b>\n\n"
        "من هون بتحط عنوان شام كاش وسيريتل كاش والوصف لكل وحدة.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_payment_method_page(query, method_id):
    with db() as conn:
        m = conn.execute("SELECT * FROM payment_methods WHERE id = ?", (method_id,)).fetchone()

    if not m:
        await query.answer("طريقة الدفع غير موجودة.", show_alert=True)
        return

    await query.edit_message_text(
        f"💳 <b>{html.escape(m['name'])}</b>\n\n"
        f"📍 العنوان:\n<code>{html.escape(m['address'] or 'غير محدد')}</code>\n\n"
        f"📝 الوصف:\n{html.escape(m['description'] or 'غير محدد')}\n\n"
        f"📌 {'مفعلة ✅' if m['active'] else 'متوقفة ⛔'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("📍 تعديل العنوان", f"admin_method_address:{method_id}")],
                [blue("📝 تعديل الوصف", f"admin_method_description:{method_id}")],
                [red("⏯ تفعيل / إيقاف", f"admin_method_toggle:{method_id}")],
                [red("⬅️ طرق الدفع", "admin_payment_methods")],
            ]
        ),
    )


async def admin_payment_requests(query):
    with db() as conn:
        rows_db = conn.execute(
            """
            SELECT pr.*, u.first_name, p.name AS package_name, pm.name AS method_name
            FROM payment_requests pr
            LEFT JOIN users u ON u.user_id = pr.user_id
            JOIN packages p ON p.id = pr.package_id
            JOIN payment_methods pm ON pm.id = pr.method_id
            WHERE pr.status = 'pending'
            ORDER BY pr.id DESC
            LIMIT 30
            """
        ).fetchall()

    rows = []
    for r in rows_db:
        name = r["first_name"] or str(r["user_id"])
        rows.append([
            blue(
                f"📥 #{r['id']} | {name} | {r['amount']:.2f}$",
                f"admin_payment_request:{r['id']}",
            )
        ])

    if not rows:
        rows.append([blue("✅ لا توجد طلبات معلقة", "noop")])

    rows.append([red("⬅️ إدارة الصانع", "factory_admin")])

    await query.edit_message_text(
        "📥 <b>طلبات الدفع المعلقة</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_payment_request_page(query, request_id):
    with db() as conn:
        r = conn.execute(
            """
            SELECT pr.*, u.first_name, u.username,
                   p.name AS package_name, p.duration_days, p.bot_limit,
                   pm.name AS method_name
            FROM payment_requests pr
            LEFT JOIN users u ON u.user_id = pr.user_id
            JOIN packages p ON p.id = pr.package_id
            JOIN payment_methods pm ON pm.id = pr.method_id
            WHERE pr.id = ?
            """,
            (request_id,),
        ).fetchone()

    if not r:
        await query.answer("الطلب غير موجود.", show_alert=True)
        return

    text = (
        f"📥 <b>طلب دفع #{r['id']}</b>\n\n"
        f"👤 {html.escape(r['first_name'] or '')}\n"
        f"🔗 @{html.escape(r['username'] or '-')}\n"
        f"🆔 <code>{r['user_id']}</code>\n"
        f"🤖 الباقة: {html.escape(r['package_name'])}\n"
        f"💳 الطريقة: {html.escape(r['method_name'])}\n"
        f"💵 المبلغ: <b>{r['amount']:.2f}$</b>\n"
        f"📌 الحالة: {html.escape(r['status'])}"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                blue("✅ قبول", f"payment_approve:{request_id}"),
                red("❌ رفض", f"payment_reject:{request_id}"),
            ],
            [red("⬅️ الطلبات", "admin_payment_requests")],
        ]
    )

    if query.message and (query.message.photo or query.message.document):
        await query.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    else:
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )


async def admin_all_bots(query):
    with db() as conn:
        rows_db = conn.execute(
            """
            SELECT cb.*, u.first_name
            FROM customer_bots cb
            LEFT JOIN users u ON u.user_id = cb.owner_user_id
            ORDER BY cb.id DESC
            LIMIT 40
            """
        ).fetchall()

    rows = []
    for row in rows_db:
        rows.append([
            blue(
                f"{'✅' if row['active'] else '⛔'} @{row['bot_username']} | {row['owner_user_id']}",
                f"admin_bot:{row['id']}",
            )
        ])

    if not rows:
        rows.append([blue("📭 لا توجد بوتات", "noop")])

    rows.append([red("⬅️ إدارة الصانع", "factory_admin")])
    await query.edit_message_text(
        "🤖 <b>بوتات العملاء</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_users(query):
    with db() as conn:
        rows_db = conn.execute(
            """
            SELECT u.*,
                   (SELECT COUNT(*) FROM customer_bots cb WHERE cb.owner_user_id = u.user_id) AS bots_count
            FROM users u
            ORDER BY u.created_at DESC
            LIMIT 40
            """
        ).fetchall()

    rows = []
    for row in rows_db:
        name = row["first_name"] or row["username"] or str(row["user_id"])
        rows.append([
            blue(
                f"{'⛔' if row['banned'] else '👤'} {name[:22]} | {row['bots_count']} بوت",
                f"admin_user:{row['user_id']}",
            )
        ])

    rows.append([red("⬅️ إدارة الصانع", "factory_admin")])

    await query.edit_message_text(
        "👥 <b>العملاء</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def admin_user_page(query, user_id):
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        bots_count = conn.execute(
            "SELECT COUNT(*) AS c FROM customer_bots WHERE owner_user_id = ?",
            (user_id,),
        ).fetchone()["c"]

    if not row:
        await query.answer("المستخدم غير موجود.", show_alert=True)
        return

    sub = active_subscription(user_id)
    sub_text = "لا يوجد"
    if sub:
        sub_text = parse_dt(sub["end_at"]).strftime("%Y-%m-%d")

    await query.edit_message_text(
        "👤 <b>العميل</b>\n\n"
        f"الاسم: {html.escape(row['first_name'] or '')}\n"
        f"اليوزر: @{html.escape(row['username'] or '-')}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"🤖 البوتات: <b>{bots_count}</b>\n"
        f"📅 الاشتراك حتى: <b>{sub_text}</b>\n"
        f"📌 {'محظور ⛔' if row['banned'] else 'فعال ✅'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [red("⛔/✅ حظر أو فك الحظر", f"admin_user_toggle:{user_id}")],
                [red("⬅️ العملاء", "admin_users")],
            ]
        ),
    )


async def admin_subscriptions(query):
    with db() as conn:
        rows_db = conn.execute(
            """
            SELECT s.*, u.first_name, p.name AS package_name
            FROM subscriptions s
            LEFT JOIN users u ON u.user_id = s.user_id
            JOIN packages p ON p.id = s.package_id
            WHERE s.active = 1
            ORDER BY s.end_at ASC
            LIMIT 50
            """
        ).fetchall()

    parts = ["📅 <b>الاشتراكات</b>\n"]
    if not rows_db:
        parts.append("\nلا توجد اشتراكات.")
    else:
        for r in rows_db:
            parts.append(
                f"\n👤 {html.escape(r['first_name'] or str(r['user_id']))}"
                f" — {html.escape(r['package_name'])}\n"
                f"حتى: <b>{html.escape(r['end_at'][:10])}</b>"
            )

    await query.edit_message_text(
        "\n".join(parts),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[red("⬅️ إدارة الصانع", "factory_admin")]]),
    )


# =========================================================
# SUBSCRIPTION APPROVAL
# =========================================================

def activate_or_extend_subscription(user_id, package_id):
    with db() as conn:
        package = conn.execute(
            "SELECT * FROM packages WHERE id = ?",
            (package_id,),
        ).fetchone()

        if not package:
            raise RuntimeError("Package not found")

        current = conn.execute(
            """
            SELECT * FROM subscriptions
            WHERE user_id = ? AND active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()

        now = now_dt()
        base = now

        if current:
            current_end = parse_dt(current["end_at"])
            if current_end and current_end > now:
                base = current_end

        new_end = base + timedelta(days=int(package["duration_days"]))
        now_s = now_iso()

        if current:
            conn.execute(
                """
                UPDATE subscriptions
                SET package_id = ?, start_at = ?, end_at = ?, active = 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    package_id,
                    current["start_at"],
                    new_end.isoformat(timespec="seconds"),
                    now_s,
                    current["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO subscriptions
                (user_id, package_id, start_at, end_at, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    user_id,
                    package_id,
                    now_s,
                    new_end.isoformat(timespec="seconds"),
                    now_s,
                    now_s,
                ),
            )

    return package, new_end


# =========================================================
# CALLBACKS
# =========================================================

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    ensure_user(query.from_user)

    if is_banned(query.from_user.id) and not is_owner(query.from_user.id):
        await query.answer("⛔ حسابك محظور.", show_alert=True)
        return

    data = query.data
    await query.answer()

    # CUSTOMER
    if data == "home":
        context.user_data.clear()
        await query.edit_message_text(
            f"🤖 <b>{html.escape(FACTORY_NAME)}</b>\n\nاختر المطلوب 👇",
            parse_mode="HTML",
            reply_markup=main_menu(query.from_user.id),
        )

    elif data == "buy_bot":
        await show_packages(query, "purchase")

    elif data == "renew":
        await show_packages(query, "renewal")

    elif data.startswith("package:"):
        _, request_type, package_id = data.split(":")
        await show_payment_methods(query, request_type, int(package_id))

    elif data.startswith("paymethod:"):
        _, request_type, package_id, method_id = data.split(":")
        await begin_payment(
            query,
            context,
            request_type,
            int(package_id),
            int(method_id),
        )

    elif data == "my_payments":
        await show_my_payments(query)

    elif data == "create_bot":
        if not active_subscription(query.from_user.id):
            await query.answer("❌ لازم تشتري اشتراك أولاً.", show_alert=True)
            return
        if remaining_bot_slots(query.from_user.id) <= 0:
            await query.answer("❌ وصلت للحد المسموح من البوتات.", show_alert=True)
            return

        context.user_data.clear()
        context.user_data["state"] = "waiting_bot_token"

        await query.edit_message_text(
            "➕ <b>إنشاء بوت جديد</b>\n\n"
            "أنشئ بوت من @BotFather وبعدين أرسل Token البوت هون.\n\n"
            "🔐 التوكن يتم حفظه مشفّر.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "home")]]),
        )

    elif data == "my_bots":
        await show_my_bots_query(query)

    elif data.startswith("customer_bot:"):
        await show_customer_bot(query, int(data.split(":", 1)[1]))

    elif data.startswith("customer_bot_toggle:"):
        bot_record_id = int(data.split(":", 1)[1])
        row = get_customer_bot(bot_record_id, owner_user_id=query.from_user.id)

        if not row:
            await query.answer("البوت غير موجود.", show_alert=True)
            return

        with db() as conn:
            conn.execute(
                "UPDATE customer_bots SET active = ?, updated_at = ? WHERE id = ?",
                (0 if row["active"] else 1, now_iso(), bot_record_id),
            )
        await show_customer_bot(query, bot_record_id)

    elif data.startswith("customer_bot_delete:"):
        bot_record_id = int(data.split(":", 1)[1])
        row = get_customer_bot(bot_record_id, owner_user_id=query.from_user.id)

        if not row:
            await query.answer("البوت غير موجود.", show_alert=True)
            return

        with db() as conn:
            conn.execute("DELETE FROM customer_bots WHERE id = ?", (bot_record_id,))
        await query.answer("✅ تم حذف البوت.", show_alert=True)
        await show_my_bots_query(query)

    elif data == "support":
        support = get_setting("support_username", SUPPORT_USERNAME).replace("@", "").strip()
        rows = []
        if support:
            rows.append([
                InlineKeyboardButton(
                    "🎧 مراسلة الدعم",
                    url=f"https://t.me/{support}",
                    style="primary",
                )
            ])
        rows.append([red("⬅️ الرئيسية", "home")])

        await query.edit_message_text(
            "🎧 <b>الدعم الفني</b>\n\nإذا واجهتك مشكلة تواصل معنا.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    elif data == "noop":
        await query.answer("لا يوجد شيء هنا.", show_alert=True)

    # OWNER
    elif data == "factory_admin":
        if not is_owner(query.from_user.id):
            await query.answer("❌ للمالك فقط.", show_alert=True)
            return
        context.user_data.clear()
        await query.edit_message_text(
            "⚙️ <b>إدارة الصانع</b>",
            parse_mode="HTML",
            reply_markup=admin_menu(),
        )

    elif data == "admin_stats":
        if not is_owner(query.from_user.id): return
        await admin_stats(query)

    elif data == "admin_packages":
        if not is_owner(query.from_user.id): return
        await admin_packages(query)

    elif data == "admin_package_add":
        if not is_owner(query.from_user.id): return
        context.user_data.clear()
        context.user_data["state"] = "package_name"
        await query.edit_message_text(
            "➕ أرسل اسم الباقة.",
            reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "admin_packages")]]),
        )

    elif data.startswith("admin_package:"):
        if not is_owner(query.from_user.id): return
        await admin_package_page(query, int(data.split(":", 1)[1]))

    elif data.startswith("admin_package_price:"):
        if not is_owner(query.from_user.id): return
        package_id = int(data.split(":", 1)[1])
        context.user_data.clear()
        context.user_data["state"] = "package_edit_price"
        context.user_data["package_id"] = package_id
        await query.edit_message_text(
            "💵 أرسل السعر الجديد بالدولار.",
            reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", f"admin_package:{package_id}")]]),
        )

    elif data.startswith("admin_package_toggle:"):
        if not is_owner(query.from_user.id): return
        package_id = int(data.split(":", 1)[1])
        with db() as conn:
            p = conn.execute("SELECT active FROM packages WHERE id = ?", (package_id,)).fetchone()
            if p:
                conn.execute(
                    "UPDATE packages SET active = ?, updated_at = ? WHERE id = ?",
                    (0 if p["active"] else 1, now_iso(), package_id),
                )
        await admin_package_page(query, package_id)

    elif data == "admin_payment_methods":
        if not is_owner(query.from_user.id): return
        await admin_payment_methods(query)

    elif data.startswith("admin_payment_method:"):
        if not is_owner(query.from_user.id): return
        await admin_payment_method_page(query, int(data.split(":", 1)[1]))

    elif data.startswith("admin_method_address:"):
        if not is_owner(query.from_user.id): return
        method_id = int(data.split(":", 1)[1])
        context.user_data.clear()
        context.user_data["state"] = "method_address"
        context.user_data["method_id"] = method_id
        await query.edit_message_text(
            "📍 أرسل عنوان/رقم التحويل.",
            reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", f"admin_payment_method:{method_id}")]]),
        )

    elif data.startswith("admin_method_description:"):
        if not is_owner(query.from_user.id): return
        method_id = int(data.split(":", 1)[1])
        context.user_data.clear()
        context.user_data["state"] = "method_description"
        context.user_data["method_id"] = method_id
        await query.edit_message_text(
            "📝 أرسل وصف وتعليمات الدفع.",
            reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", f"admin_payment_method:{method_id}")]]),
        )

    elif data.startswith("admin_method_toggle:"):
        if not is_owner(query.from_user.id): return
        method_id = int(data.split(":", 1)[1])
        with db() as conn:
            m = conn.execute("SELECT active FROM payment_methods WHERE id = ?", (method_id,)).fetchone()
            if m:
                conn.execute(
                    "UPDATE payment_methods SET active = ?, updated_at = ? WHERE id = ?",
                    (0 if m["active"] else 1, now_iso(), method_id),
                )
        await admin_payment_method_page(query, method_id)

    elif data == "admin_payment_requests":
        if not is_owner(query.from_user.id): return
        await admin_payment_requests(query)

    elif data.startswith("admin_payment_request:"):
        if not is_owner(query.from_user.id): return
        await admin_payment_request_page(query, int(data.split(":", 1)[1]))

    elif data.startswith("payment_approve:"):
        if not is_owner(query.from_user.id): return
        request_id = int(data.split(":", 1)[1])

        with db() as conn:
            req = conn.execute(
                "SELECT * FROM payment_requests WHERE id = ?",
                (request_id,),
            ).fetchone()

        if not req or req["status"] != "pending":
            await query.answer("تمت معالجة الطلب مسبقاً.", show_alert=True)
            return

        package, new_end = activate_or_extend_subscription(
            req["user_id"],
            req["package_id"],
        )

        with db() as conn:
            conn.execute(
                """
                UPDATE payment_requests
                SET status = 'approved', admin_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (query.from_user.id, now_iso(), request_id),
            )

        try:
            await context.bot.send_message(
                req["user_id"],
                f"✅ <b>تم قبول دفعتك #{request_id}</b>\n\n"
                f"🤖 الباقة: {html.escape(package['name'])}\n"
                f"📅 اشتراكك فعال حتى: <b>{new_end.strftime('%Y-%m-%d')}</b>\n\n"
                "صار فيك هلق تنشئ بوتك من الصانع.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[blue("➕ إنشاء بوتي", "create_bot")]]),
            )
        except TelegramError:
            pass

        await query.answer("✅ تم قبول الدفع وتفعيل الاشتراك.", show_alert=True)
        await admin_payment_requests(query)

    elif data.startswith("payment_reject:"):
        if not is_owner(query.from_user.id): return
        request_id = int(data.split(":", 1)[1])

        with db() as conn:
            req = conn.execute(
                "SELECT * FROM payment_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            if not req or req["status"] != "pending":
                await query.answer("تمت معالجة الطلب مسبقاً.", show_alert=True)
                return
            conn.execute(
                """
                UPDATE payment_requests
                SET status = 'rejected', admin_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (query.from_user.id, now_iso(), request_id),
            )

        try:
            await context.bot.send_message(
                req["user_id"],
                f"❌ تم رفض طلب الدفع #{request_id}. تواصل مع الدعم إذا احتجت.",
            )
        except TelegramError:
            pass

        await query.answer("تم رفض الطلب.", show_alert=True)
        await admin_payment_requests(query)

    elif data == "admin_all_bots":
        if not is_owner(query.from_user.id): return
        await admin_all_bots(query)

    elif data == "admin_users":
        if not is_owner(query.from_user.id): return
        await admin_users(query)

    elif data.startswith("admin_user:"):
        if not is_owner(query.from_user.id): return
        await admin_user_page(query, int(data.split(":", 1)[1]))

    elif data.startswith("admin_user_toggle:"):
        if not is_owner(query.from_user.id): return
        user_id = int(data.split(":", 1)[1])

        if user_id == FACTORY_OWNER_ID:
            await query.answer("❌ لا يمكن حظر المالك.", show_alert=True)
            return

        with db() as conn:
            row = conn.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET banned = ?, updated_at = ? WHERE user_id = ?",
                    (0 if row["banned"] else 1, now_iso(), user_id),
                )
        await admin_user_page(query, user_id)

    elif data == "admin_subscriptions":
        if not is_owner(query.from_user.id): return
        await admin_subscriptions(query)

    elif data == "admin_broadcast":
        if not is_owner(query.from_user.id): return
        context.user_data.clear()
        context.user_data["state"] = "admin_broadcast"
        await query.edit_message_text(
            "📢 أرسل الرسالة التي تريد إرسالها لكل العملاء.",
            reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "factory_admin")]]),
        )

    elif data == "admin_support":
        if not is_owner(query.from_user.id): return
        context.user_data.clear()
        context.user_data["state"] = "admin_support"
        await query.edit_message_text(
            "🎧 أرسل يوزر الدعم بدون @.",
            reply_markup=InlineKeyboardMarkup([[red("❌ إلغاء", "factory_admin")]]),
        )


# =========================================================
# TEXT STATES
# =========================================================

TOKEN_RE = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{20,}$")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user = update.effective_user
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")

    if is_banned(user.id) and not is_owner(user.id):
        await update.message.reply_text("⛔ حسابك محظور.")
        return

    if not state:
        return

    if state == "waiting_bot_token":
        if not active_subscription(user.id):
            context.user_data.clear()
            await update.message.reply_text(
                "❌ اشتراكك غير فعال.",
                reply_markup=main_menu(user.id),
            )
            return

        if remaining_bot_slots(user.id) <= 0:
            context.user_data.clear()
            await update.message.reply_text(
                "❌ وصلت للحد المسموح من البوتات.",
                reply_markup=main_menu(user.id),
            )
            return

        if not TOKEN_RE.match(text):
            await update.message.reply_text(
                "❌ التوكن شكله غير صحيح. انسخه كامل من @BotFather."
            )
            return

        try:
            temp_bot = Bot(token=text)
            me = await temp_bot.get_me()
        except TelegramError:
            await update.message.reply_text(
                "❌ ما قدرت أتأكد من التوكن. تأكد إنه صحيح وفعال."
            )
            return

        encrypted = encrypt_token(text)
        now = now_iso()

        try:
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO customer_bots
                    (
                        owner_user_id, telegram_bot_id, bot_username, bot_name,
                        token_encrypted, active, template_status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 1, 'registered', ?, ?)
                    """,
                    (
                        user.id,
                        me.id,
                        me.username or "",
                        me.first_name or me.username or str(me.id),
                        encrypted,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError:
            context.user_data.clear()
            await update.message.reply_text(
                "⚠️ هذا البوت مسجل بالصانع من قبل.",
                reply_markup=main_menu(user.id),
            )
            return

        context.user_data.clear()
        await update.message.reply_text(
            "✅ <b>تم تسجيل البوت بنجاح</b>\n\n"
            f"🤖 {html.escape(me.first_name or '')}\n"
            f"🔗 @{html.escape(me.username or '')}\n\n"
            "بالمرحلة التالية منربط قالب المتجر عليه.",
            parse_mode="HTML",
            reply_markup=main_menu(user.id),
        )
        return

    if not is_owner(user.id):
        return

    if state == "package_name":
        context.user_data["package_name"] = text
        context.user_data["state"] = "package_price"
        await update.message.reply_text("💵 أرسل سعر الباقة بالدولار.")
        return

    if state == "package_price":
        try:
            price = float(text.replace(",", "."))
            if price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ أرسل سعر صحيح.")
            return
        context.user_data["package_price"] = price
        context.user_data["state"] = "package_days"
        await update.message.reply_text("📅 أرسل مدة الاشتراك بالأيام. مثال: 30")
        return

    if state == "package_days":
        try:
            days = int(text)
            if days <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ أرسل عدد أيام صحيح.")
            return
        context.user_data["package_days"] = days
        context.user_data["state"] = "package_bot_limit"
        await update.message.reply_text("🤖 كم بوت يسمح له الاشتراك؟ مثال: 1")
        return

    if state == "package_bot_limit":
        try:
            bot_limit = int(text)
            if bot_limit <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ أرسل عدد صحيح.")
            return

        name = context.user_data["package_name"]
        price = context.user_data["package_price"]
        days = context.user_data["package_days"]

        with db() as conn:
            conn.execute(
                """
                INSERT INTO packages
                (name, price, duration_days, bot_limit, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (name, price, days, bot_limit, now_iso(), now_iso()),
            )

        context.user_data.clear()
        await update.message.reply_text(
            "✅ تم إضافة الباقة.",
            reply_markup=InlineKeyboardMarkup([[blue("💰 الباقات", "admin_packages")]]),
        )
        return

    if state == "package_edit_price":
        try:
            price = float(text.replace(",", "."))
            if price <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ أرسل سعر صحيح.")
            return

        package_id = context.user_data["package_id"]
        with db() as conn:
            conn.execute(
                "UPDATE packages SET price = ?, updated_at = ? WHERE id = ?",
                (price, now_iso(), package_id),
            )

        context.user_data.clear()
        await update.message.reply_text(
            "✅ تم تعديل السعر.",
            reply_markup=InlineKeyboardMarkup([[blue("💰 فتح الباقة", f"admin_package:{package_id}")]]),
        )
        return

    if state == "method_address":
        method_id = context.user_data["method_id"]
        with db() as conn:
            conn.execute(
                "UPDATE payment_methods SET address = ?, updated_at = ? WHERE id = ?",
                (text, now_iso(), method_id),
            )
        context.user_data.clear()
        await update.message.reply_text(
            "✅ تم حفظ عنوان التحويل.",
            reply_markup=InlineKeyboardMarkup([[blue("💳 فتح الطريقة", f"admin_payment_method:{method_id}")]]),
        )
        return

    if state == "method_description":
        method_id = context.user_data["method_id"]
        with db() as conn:
            conn.execute(
                "UPDATE payment_methods SET description = ?, updated_at = ? WHERE id = ?",
                (text, now_iso(), method_id),
            )
        context.user_data.clear()
        await update.message.reply_text(
            "✅ تم حفظ الوصف.",
            reply_markup=InlineKeyboardMarkup([[blue("💳 فتح الطريقة", f"admin_payment_method:{method_id}")]]),
        )
        return

    if state == "admin_broadcast":
        with db() as conn:
            users = conn.execute("SELECT user_id FROM users WHERE banned = 0").fetchall()

        sent = 0
        failed = 0
        for row in users:
            try:
                await context.bot.send_message(row["user_id"], text)
                sent += 1
            except TelegramError:
                failed += 1

        context.user_data.clear()
        await update.message.reply_text(
            f"📢 انتهت الإذاعة.\n✅ تم: {sent}\n❌ فشل: {failed}",
            reply_markup=InlineKeyboardMarkup([[blue("⚙️ الإدارة", "factory_admin")]]),
        )
        return

    if state == "admin_support":
        username = text.replace("@", "").strip()
        set_setting("support_username", username)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ تم تحديث الدعم إلى @{username}",
            reply_markup=InlineKeyboardMarkup([[blue("⚙️ الإدارة", "factory_admin")]]),
        )


# =========================================================
# PHOTO PROOF
# =========================================================

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)

    if context.user_data.get("state") != "waiting_payment_proof":
        return

    user = update.effective_user
    request_type = context.user_data.get("request_type")
    package_id = context.user_data.get("package_id")
    method_id = context.user_data.get("method_id")

    with db() as conn:
        package = conn.execute(
            "SELECT * FROM packages WHERE id = ?",
            (package_id,),
        ).fetchone()
        method = conn.execute(
            "SELECT * FROM payment_methods WHERE id = ?",
            (method_id,),
        ).fetchone()

    if not package or not method:
        context.user_data.clear()
        await update.message.reply_text("❌ حصل خطأ. حاول من جديد.")
        return

    photo = update.message.photo[-1]
    now = now_iso()

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO payment_requests
            (user_id, package_id, method_id, request_type, amount, proof_file_id,
             status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                user.id,
                package_id,
                method_id,
                request_type,
                package["price"],
                photo.file_id,
                now,
                now,
            ),
        )
        request_id = cur.lastrowid

    context.user_data.clear()

    await update.message.reply_text(
        f"✅ <b>تم إرسال طلب الدفع #{request_id}</b>\n\n"
        f"🤖 {html.escape(package['name'])}\n"
        f"💳 {html.escape(method['name'])}\n"
        f"💵 {package['price']:.2f}$\n"
        "⏳ بانتظار مراجعة الإدارة.",
        parse_mode="HTML",
        reply_markup=main_menu(user.id),
    )

    try:
        await context.bot.send_photo(
            chat_id=FACTORY_OWNER_ID,
            photo=photo.file_id,
            caption=(
                f"📥 طلب دفع جديد #{request_id}\n"
                f"👤 {user.first_name or ''}\n"
                f"🆔 {user.id}\n"
                f"🤖 {package['name']}\n"
                f"💳 {method['name']}\n"
                f"💵 {package['price']:.2f}$"
            ),
            reply_markup=InlineKeyboardMarkup(
                [[blue("فتح الطلب", f"admin_payment_request:{request_id}")]]
            ),
        )
    except TelegramError as exc:
        logger.warning("Could not notify owner: %s", exc)


# =========================================================
# ERROR / RUN
# =========================================================

async def error_handler(update, context):
    logger.exception("Unhandled error", exc_info=context.error)


def main():
    if not FACTORY_BOT_TOKEN:
        raise RuntimeError("FACTORY_BOT_TOKEN غير موجود في Railway Variables.")
    if not FACTORY_OWNER_ID:
        raise RuntimeError("FACTORY_OWNER_ID غير موجود في Railway Variables.")

    init_db()

    app = (
        Application.builder()
        .token(FACTORY_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("mybots", my_bots_command))
    app.add_handler(CommandHandler("admin", admin_command))

    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.add_error_handler(error_handler)

    print(f"{FACTORY_NAME} FACTORY V2 IS RUNNING...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
