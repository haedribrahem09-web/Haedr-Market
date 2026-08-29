import os
import re
import html
import base64
import hashlib
import sqlite3
import logging
from datetime import datetime

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

# نشتق مفتاح تشفير ثابت من FACTORY_SECRET_KEY، وإن لم يوجد من توكن الصانع نفسه.
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

def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


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
            """
        )

        defaults = {
            "support_username": SUPPORT_USERNAME,
            "subscription_text": "نظام الاشتراك سيتم تفعيله قريباً.",
        }

        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
                (key, value),
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
            (
                user.id,
                user.username or "",
                user.first_name or "",
                now,
                now,
            ),
        )

        conn.execute(
            """
            UPDATE users
            SET username = ?, first_name = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (
                user.username or "",
                user.first_name or "",
                now,
                user.id,
            ),
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
            SELECT *
            FROM customer_bots
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
            SELECT *
            FROM customer_bots
            WHERE id = ? AND owner_user_id = ?
            """,
            (bot_id, owner_user_id),
        ).fetchone()


# =========================================================
# UI
# =========================================================

def blue(text, data):
    return InlineKeyboardButton(
        text=text,
        callback_data=data,
        style="primary",
    )


def red(text, data):
    return InlineKeyboardButton(
        text=text,
        callback_data=data,
        style="danger",
    )


def main_menu(user_id):
    rows = [
        [blue("➕ إنشاء بوت جديد", "create_bot")],
        [
            blue("🤖 بوتاتي", "my_bots"),
            red("💳 الاشتراك", "subscription"),
        ],
        [blue("🎧 الدعم الفني", "support")],
    ]

    if is_owner(user_id):
        rows.append([red("⚙️ إدارة الصانع", "factory_admin")])

    return InlineKeyboardMarkup(rows)


def back_home():
    return InlineKeyboardMarkup(
        [[red("⬅️ الرئيسية", "home")]]
    )


def admin_menu():
    return InlineKeyboardMarkup(
        [
            [
                blue("📊 الإحصائيات", "admin_stats"),
                red("🤖 كل البوتات", "admin_all_bots"),
            ],
            [
                blue("👥 المستخدمون", "admin_users"),
                red("📢 إذاعة", "admin_broadcast"),
            ],
            [
                blue("🎧 تعديل الدعم", "admin_support"),
                red("💳 نص الاشتراك", "admin_subscription"),
            ],
            [red("⬅️ الرئيسية", "home")],
        ]
    )


# =========================================================
# BOT PROFILE
# =========================================================

async def post_init(application):
    try:
        await application.bot.set_my_description(
            description=(
                f"{FACTORY_NAME} 🤖\n"
                "أنشئ وأدر بوتاتك من مكان واحد بسهولة."
            )
        )

        await application.bot.set_my_short_description(
            short_description="صانع وإدارة بوتات تيليجرام"
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


# =========================================================
# COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text("⛔ حسابك محظور من استخدام الصانع.")
        return

    context.user_data.clear()

    await update.message.reply_text(
        f"👋 <b>أهلاً {html.escape(user.first_name or 'صديقي')}</b>\n\n"
        f"🤖 <b>{html.escape(FACTORY_NAME)}</b>\n\n"
        "من هون بتقدر تنشئ وتدير بوتاتك بسهولة.\n\n"
        "اختر المطلوب 👇",
        parse_mode="HTML",
        reply_markup=main_menu(user.id),
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)

    await update.message.reply_text(
        f"🆔 <b>معرف حسابك:</b>\n\n"
        f"<code>{update.effective_user.id}</code>",
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
# CUSTOMER PAGES
# =========================================================

async def show_my_bots_message(message, user_id):
    bots = user_bots(user_id)

    if not bots:
        await message.reply_text(
            "🤖 <b>بوتاتي</b>\n\n"
            "ما عندك بوتات مسجلة حالياً.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [blue("➕ إنشاء أول بوت", "create_bot")],
                    [red("⬅️ الرئيسية", "home")],
                ]
            ),
        )
        return

    rows = []

    for row in bots[:30]:
        icon = "✅" if row["active"] else "⛔"
        rows.append(
            [
                blue(
                    f"{icon} @{row['bot_username']}",
                    f"customer_bot:{row['id']}",
                )
            ]
        )

    rows.append([blue("➕ إنشاء بوت جديد", "create_bot")])
    rows.append([red("⬅️ الرئيسية", "home")])

    await message.reply_text(
        f"🤖 <b>بوتاتي</b>\n\n"
        f"عدد البوتات: <b>{len(bots)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_my_bots_query(query):
    bots = user_bots(query.from_user.id)
    rows = []

    for row in bots[:30]:
        icon = "✅" if row["active"] else "⛔"
        rows.append(
            [
                blue(
                    f"{icon} @{row['bot_username']}",
                    f"customer_bot:{row['id']}",
                )
            ]
        )

    if not rows:
        rows.append([blue("📭 ما عندك بوتات", "noop")])

    rows.append([blue("➕ إنشاء بوت جديد", "create_bot")])
    rows.append([red("⬅️ الرئيسية", "home")])

    await query.edit_message_text(
        f"🤖 <b>بوتاتي</b>\n\nعدد البوتات: <b>{len(bots)}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def show_customer_bot(query, bot_record_id):
    row = get_customer_bot(
        bot_record_id,
        owner_user_id=query.from_user.id,
    )

    if not row:
        await query.answer("البوت غير موجود.", show_alert=True)
        return

    status = "فعال ✅" if row["active"] else "متوقف ⛔"

    await query.edit_message_text(
        f"🤖 <b>{html.escape(row['bot_name'])}</b>\n\n"
        f"🔗 @{html.escape(row['bot_username'])}\n"
        f"🆔 <code>{row['telegram_bot_id']}</code>\n"
        f"📌 الحالة: <b>{status}</b>\n\n"
        "تم تسجيل البوت بالصانع بنجاح.\n"
        "بالخطوة القادمة رح نربط قالب المتجر فيه.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("⏯ تفعيل / إيقاف", f"customer_bot_toggle:{row['id']}")],
                [red("🗑 حذف البوت من الصانع", f"customer_bot_delete:{row['id']}")],
                [red("⬅️ بوتاتي", "my_bots")],
            ]
        ),
    )


# =========================================================
# ADMIN PAGES
# =========================================================

async def admin_stats(query):
    with db() as conn:
        users = conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        bots = conn.execute(
            "SELECT COUNT(*) AS c FROM customer_bots"
        ).fetchone()["c"]

        active = conn.execute(
            "SELECT COUNT(*) AS c FROM customer_bots WHERE active = 1"
        ).fetchone()["c"]

    await query.edit_message_text(
        "📊 <b>إحصائيات الصانع</b>\n\n"
        f"👥 المستخدمون: <b>{users}</b>\n"
        f"🤖 البوتات: <b>{bots}</b>\n"
        f"✅ البوتات الفعالة: <b>{active}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [[red("⬅️ إدارة الصانع", "factory_admin")]]
        ),
    )


async def admin_all_bots(query):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT cb.*, u.first_name
            FROM customer_bots cb
            LEFT JOIN users u ON u.user_id = cb.owner_user_id
            ORDER BY cb.id DESC
            LIMIT 40
            """
        ).fetchall()

    keyboard = []

    for row in rows:
        icon = "✅" if row["active"] else "⛔"
        keyboard.append(
            [
                blue(
                    f"{icon} @{row['bot_username']} | {row['owner_user_id']}",
                    f"admin_bot:{row['id']}",
                )
            ]
        )

    if not keyboard:
        keyboard.append([blue("📭 لا توجد بوتات", "noop")])

    keyboard.append([red("⬅️ إدارة الصانع", "factory_admin")])

    await query.edit_message_text(
        "🤖 <b>كل البوتات المسجلة</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_bot_page(query, bot_record_id):
    row = get_customer_bot(bot_record_id)

    if not row:
        await query.answer("البوت غير موجود.", show_alert=True)
        return

    await query.edit_message_text(
        f"🤖 <b>{html.escape(row['bot_name'])}</b>\n\n"
        f"🔗 @{html.escape(row['bot_username'])}\n"
        f"👤 صاحب البوت: <code>{row['owner_user_id']}</code>\n"
        f"🆔 Bot ID: <code>{row['telegram_bot_id']}</code>\n"
        f"📌 {'فعال ✅' if row['active'] else 'متوقف ⛔'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [blue("⏯ تفعيل / إيقاف", f"admin_bot_toggle:{row['id']}")],
                [red("🗑 حذف", f"admin_bot_delete:{row['id']}")],
                [red("⬅️ كل البوتات", "admin_all_bots")],
            ]
        ),
    )


async def admin_users(query):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT u.*,
                   (SELECT COUNT(*) FROM customer_bots cb WHERE cb.owner_user_id = u.user_id) AS bots_count
            FROM users u
            ORDER BY u.created_at DESC
            LIMIT 40
            """
        ).fetchall()

    keyboard = []

    for row in rows:
        name = row["first_name"] or row["username"] or str(row["user_id"])
        icon = "⛔" if row["banned"] else "👤"
        keyboard.append(
            [
                blue(
                    f"{icon} {name[:22]} | {row['bots_count']} بوت",
                    f"admin_user:{row['user_id']}",
                )
            ]
        )

    keyboard.append([red("⬅️ إدارة الصانع", "factory_admin")])

    await query.edit_message_text(
        "👥 <b>مستخدمو الصانع</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_user_page(query, user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        bots_count = conn.execute(
            "SELECT COUNT(*) AS c FROM customer_bots WHERE owner_user_id = ?",
            (user_id,),
        ).fetchone()["c"]

    if not row:
        await query.answer("المستخدم غير موجود.", show_alert=True)
        return

    await query.edit_message_text(
        "👤 <b>المستخدم</b>\n\n"
        f"الاسم: {html.escape(row['first_name'] or '')}\n"
        f"اليوزر: @{html.escape(row['username'] or '-')}\n"
        f"🆔 <code>{user_id}</code>\n"
        f"🤖 عدد البوتات: <b>{bots_count}</b>\n"
        f"📌 {'محظور ⛔' if row['banned'] else 'فعال ✅'}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            [
                [red("⛔/✅ حظر أو فك الحظر", f"admin_user_toggle:{user_id}")],
                [red("⬅️ المستخدمون", "admin_users")],
            ]
        ),
    )


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
            f"🤖 <b>{html.escape(FACTORY_NAME)}</b>\n\n"
            "اختر المطلوب 👇",
            parse_mode="HTML",
            reply_markup=main_menu(query.from_user.id),
        )

    elif data == "create_bot":
        context.user_data.clear()
        context.user_data["state"] = "waiting_bot_token"

        await query.edit_message_text(
            "➕ <b>إنشاء بوت جديد</b>\n\n"
            "1️⃣ افتح @BotFather\n"
            "2️⃣ أنشئ بوت جديد\n"
            "3️⃣ أرسل لي <b>Token</b> البوت هون.\n\n"
            "🔐 التوكن يتم حفظه مشفّر داخل الصانع.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[red("❌ إلغاء", "home")]]
            ),
        )

    elif data == "my_bots":
        await show_my_bots_query(query)

    elif data.startswith("customer_bot:"):
        await show_customer_bot(
            query,
            int(data.split(":", 1)[1]),
        )

    elif data.startswith("customer_bot_toggle:"):
        bot_record_id = int(data.split(":", 1)[1])

        row = get_customer_bot(
            bot_record_id,
            owner_user_id=query.from_user.id,
        )

        if not row:
            await query.answer("البوت غير موجود.", show_alert=True)
            return

        with db() as conn:
            conn.execute(
                """
                UPDATE customer_bots
                SET active = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    0 if row["active"] else 1,
                    now_iso(),
                    bot_record_id,
                ),
            )

        await show_customer_bot(query, bot_record_id)

    elif data.startswith("customer_bot_delete:"):
        bot_record_id = int(data.split(":", 1)[1])

        row = get_customer_bot(
            bot_record_id,
            owner_user_id=query.from_user.id,
        )

        if not row:
            await query.answer("البوت غير موجود.", show_alert=True)
            return

        with db() as conn:
            conn.execute(
                "DELETE FROM customer_bots WHERE id = ?",
                (bot_record_id,),
            )

        await query.answer("✅ تم حذف البوت من الصانع.", show_alert=True)
        await show_my_bots_query(query)

    elif data == "subscription":
        text = get_setting(
            "subscription_text",
            "نظام الاشتراك سيتم تفعيله قريباً.",
        )

        await query.edit_message_text(
            f"💳 <b>الاشتراك</b>\n\n{html.escape(text)}",
            parse_mode="HTML",
            reply_markup=back_home(),
        )

    elif data == "support":
        support = get_setting(
            "support_username",
            SUPPORT_USERNAME,
        ).replace("@", "").strip()

        rows = []

        if support:
            rows.append(
                [
                    InlineKeyboardButton(
                        "🎧 مراسلة الدعم",
                        url=f"https://t.me/{support}",
                        style="primary",
                    )
                ]
            )

        rows.append([red("⬅️ الرئيسية", "home")])

        await query.edit_message_text(
            "🎧 <b>الدعم الفني</b>\n\n"
            "إذا واجهتك مشكلة تواصل معنا.",
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

    elif data.startswith("admin_"):
        if not is_owner(query.from_user.id):
            await query.answer("❌ للمالك فقط.", show_alert=True)
            return

        if data == "admin_stats":
            await admin_stats(query)

        elif data == "admin_all_bots":
            await admin_all_bots(query)

        elif data.startswith("admin_bot:"):
            await admin_bot_page(
                query,
                int(data.split(":", 1)[1]),
            )

        elif data.startswith("admin_bot_toggle:"):
            bot_record_id = int(data.split(":", 1)[1])
            row = get_customer_bot(bot_record_id)

            if row:
                with db() as conn:
                    conn.execute(
                        """
                        UPDATE customer_bots
                        SET active = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            0 if row["active"] else 1,
                            now_iso(),
                            bot_record_id,
                        ),
                    )

            await admin_bot_page(query, bot_record_id)

        elif data.startswith("admin_bot_delete:"):
            bot_record_id = int(data.split(":", 1)[1])

            with db() as conn:
                conn.execute(
                    "DELETE FROM customer_bots WHERE id = ?",
                    (bot_record_id,),
                )

            await query.answer("✅ تم الحذف.", show_alert=True)
            await admin_all_bots(query)

        elif data == "admin_users":
            await admin_users(query)

        elif data.startswith("admin_user:"):
            await admin_user_page(
                query,
                int(data.split(":", 1)[1]),
            )

        elif data.startswith("admin_user_toggle:"):
            user_id = int(data.split(":", 1)[1])

            if user_id == FACTORY_OWNER_ID:
                await query.answer(
                    "❌ لا يمكن حظر مالك الصانع.",
                    show_alert=True,
                )
                return

            with db() as conn:
                row = conn.execute(
                    "SELECT banned FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()

                if row:
                    conn.execute(
                        """
                        UPDATE users
                        SET banned = ?, updated_at = ?
                        WHERE user_id = ?
                        """,
                        (
                            0 if row["banned"] else 1,
                            now_iso(),
                            user_id,
                        ),
                    )

            await admin_user_page(query, user_id)

        elif data == "admin_broadcast":
            context.user_data.clear()
            context.user_data["state"] = "admin_broadcast"

            await query.edit_message_text(
                "📢 <b>الإذاعة</b>\n\n"
                "أرسل الرسالة التي تريد إرسالها لكل مستخدمي الصانع.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[red("❌ إلغاء", "factory_admin")]]
                ),
            )

        elif data == "admin_support":
            context.user_data.clear()
            context.user_data["state"] = "admin_support"

            await query.edit_message_text(
                "🎧 أرسل يوزر الدعم الجديد بدون @.",
                reply_markup=InlineKeyboardMarkup(
                    [[red("❌ إلغاء", "factory_admin")]]
                ),
            )

        elif data == "admin_subscription":
            context.user_data.clear()
            context.user_data["state"] = "admin_subscription"

            await query.edit_message_text(
                "💳 أرسل النص الذي تريد أن يظهر داخل قسم الاشتراك.",
                reply_markup=InlineKeyboardMarkup(
                    [[red("❌ إلغاء", "factory_admin")]]
                ),
            )


# =========================================================
# TEXT STATES
# =========================================================

TOKEN_RE = re.compile(r"^\d{6,15}:[A-Za-z0-9_-]{20,}$")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    user = update.effective_user

    if is_banned(user.id) and not is_owner(user.id):
        await update.message.reply_text("⛔ حسابك محظور.")
        return

    state = context.user_data.get("state")
    text = (update.message.text or "").strip()

    if not state:
        return

    # إنشاء بوت للعميل
    if state == "waiting_bot_token":
        if not TOKEN_RE.match(text):
            await update.message.reply_text(
                "❌ التوكن شكله غير صحيح.\n"
                "انسخه كامل من @BotFather وحاول مرة ثانية."
            )
            return

        try:
            temp_bot = Bot(token=text)
            me = await temp_bot.get_me()
        except TelegramError:
            await update.message.reply_text(
                "❌ ما قدرت أتأكد من التوكن.\n"
                "تأكد إنه صحيح ولسه فعال من @BotFather."
            )
            return

        if not me.username:
            await update.message.reply_text("❌ البوت ما عنده Username.")
            return

        encrypted = encrypt_token(text)
        now = now_iso()

        try:
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO customer_bots
                    (
                        owner_user_id,
                        telegram_bot_id,
                        bot_username,
                        bot_name,
                        token_encrypted,
                        active,
                        template_status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, 1, 'registered', ?, ?)
                    """,
                    (
                        user.id,
                        me.id,
                        me.username,
                        me.first_name or me.username,
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
            "✅ <b>تم تسجيل البوت بالصانع بنجاح</b>\n\n"
            f"🤖 {html.escape(me.first_name or me.username)}\n"
            f"🔗 @{html.escape(me.username)}\n\n"
            "هلق الصانع صار جاهز يحتفظ ببوتات العملاء.\n"
            "بالمرحلة التالية منركب عليه قالب المتجر اللي عملناه.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [blue("🤖 بوتاتي", "my_bots")],
                    [red("⬅️ الرئيسية", "home")],
                ]
            ),
        )
        return

    # OWNER STATES
    if not is_owner(user.id):
        return

    if state == "admin_broadcast":
        with db() as conn:
            users = conn.execute(
                "SELECT user_id FROM users WHERE banned = 0"
            ).fetchall()

        sent = 0
        failed = 0

        for row in users:
            try:
                await context.bot.send_message(
                    row["user_id"],
                    text,
                )
                sent += 1
            except TelegramError:
                failed += 1

        context.user_data.clear()

        await update.message.reply_text(
            f"📢 انتهت الإذاعة.\n\n"
            f"✅ تم: {sent}\n"
            f"❌ فشل: {failed}",
            reply_markup=InlineKeyboardMarkup(
                [[blue("⚙️ إدارة الصانع", "factory_admin")]]
            ),
        )

    elif state == "admin_support":
        username = text.replace("@", "").strip()
        set_setting("support_username", username)
        context.user_data.clear()

        await update.message.reply_text(
            f"✅ تم تحديث الدعم إلى @{username}",
            reply_markup=InlineKeyboardMarkup(
                [[blue("⚙️ إدارة الصانع", "factory_admin")]]
            ),
        )

    elif state == "admin_subscription":
        set_setting("subscription_text", text)
        context.user_data.clear()

        await update.message.reply_text(
            "✅ تم تحديث نص الاشتراك.",
            reply_markup=InlineKeyboardMarkup(
                [[blue("⚙️ إدارة الصانع", "factory_admin")]]
            ),
        )


# =========================================================
# ERROR
# =========================================================

async def error_handler(update, context):
    logger.exception(
        "Unhandled error",
        exc_info=context.error,
    )


# =========================================================
# RUN
# =========================================================

def main():
    if not FACTORY_BOT_TOKEN:
        raise RuntimeError(
            "FACTORY_BOT_TOKEN غير موجود في Railway Variables."
        )

    if not FACTORY_OWNER_ID:
        raise RuntimeError(
            "FACTORY_OWNER_ID غير موجود في Railway Variables."
        )

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
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    app.add_error_handler(error_handler)

    print(f"{FACTORY_NAME} FACTORY IS RUNNING...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
