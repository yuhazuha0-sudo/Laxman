#!/usr/bin/env python3
"""
bot.py - Professional Telegram bot using python-telegram-bot (async).
Features:
 - Start/help/about commands
 - Inline main menu & callback handlers
 - Conversation handler for a small "contact/support" form
 - SQLite persistence for users & stats (aiosqlite)
 - Admin commands: /broadcast, /stats, /ban, /unban
 - Rate-limiting (per-user)
 - Robust logging and error handling
 - Config via environment vars or .env
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Any

from dotenv import load_dotenv
import aiosqlite

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatAction,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
)

# Load env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID") or 0)
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot_data.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set in environment")

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Rate limiting: max messages per interval
RATE_LIMIT_COUNT = 5
RATE_LIMIT_INTERVAL = timedelta(seconds=10)
_user_message_times: Dict[int, list] = {}

# Conversation states
(
    CONTACT_NAME,
    CONTACT_MESSAGE,
) = range(2)

# Helpers - database
async def init_db():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                joined_at TEXT
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS banned (
                user_id INTEGER PRIMARY KEY,
                reason TEXT,
                banned_at TEXT
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                text TEXT,
                created_at TEXT
            );
            """
        )
        await db.commit()
    logger.info("Database initialized.")


async def add_user(user):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            """
            INSERT OR REPLACE INTO users (user_id, username, first_name, last_name, joined_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user.id,
                getattr(user, "username", None),
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
                datetime.utcnow().isoformat(),
            ),
        )
        await db.commit()


async def is_banned(user_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("SELECT 1 FROM banned WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row is not None


async def ban_user(user_id: int, reason: str = ""):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO banned (user_id, reason, banned_at) VALUES (?, ?, ?)",
            (user_id, reason, datetime.utcnow().isoformat()),
        )
        await db.commit()


async def unban_user(user_id: int):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM banned WHERE user_id = ?", (user_id,))
        await db.commit()


async def log_message(user_id: int, text: str):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO messages (user_id, text, created_at) VALUES (?, ?, ?)",
            (user_id, text, datetime.utcnow().isoformat()),
        )
        await db.commit()


# Rate limiting helper
def is_rate_limited(user_id: int) -> bool:
    now = datetime.utcnow()
    times = _user_message_times.get(user_id, [])
    # keep only recent timestamps
    times = [t for t in times if now - t < RATE_LIMIT_INTERVAL]
    times.append(now)
    _user_message_times[user_id] = times
    return len(times) > RATE_LIMIT_COUNT


# UI helpers
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📣 About", callback_data="about")],
        [InlineKeyboardButton("📝 Contact / Support", callback_data="contact")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(keyboard)


def settings_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔔 Subscribe", callback_data="subscribe")],
        [InlineKeyboardButton("🔕 Unsubscribe", callback_data="unsubscribe")],
        [InlineKeyboardButton("◀️ Back", callback_data="back_main")],
    ]
    return InlineKeyboardMarkup(keyboard)


# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if await is_banned(user.id):
        await update.effective_message.reply_text(
            "आपको अस्थायी रूप से निषिद्ध कर दिया गया है। अधिक जानकारी के लिए admin से संपर्क करें।"
        )
        return

    await add_user(user)
    text = (
        f"नमस्ते, {user.mention_html()}!\n\n"
        "मैं आपका professional bot हूँ — नीचे दिए गए मेनू से शुरू करें।\n\n"
        "<b>Quick commands</b>:\n"
        "/help — जानकारी\n"
        "/about — bot के बारे में\n"
    )
    await update.effective_message.reply_html(text, reply_markup=main_menu_keyboard())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "याद रखने योग्य कमांड्स:\n"
        "/start — शुरू करें\n"
        "/help — यह मैसेज\n"
        "/about — bot जानकारी\n"
        "/contact — मेरे साथ संपर्क करें\n"
    )
    await update.effective_message.reply_text(txt)


async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "<b>Professional Bot</b>\n"
        "Version: 1.0\n"
        "Features: polished UI, sqlite persistence, admin tools, and more.\n"
    )
    await update.effective_message.reply_html(txt)


# CallbackQuery
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "about":
        await query.edit_message_text(
            text=(
                "<b>About this Bot</b>\n"
                "यह एक demonstration bot है — professional features के साथ।"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(),
        )
    elif data == "contact":
        await query.edit_message_text("कृपया अपना नाम भेजें (या /cancel):")
        return await start_contact_flow(query, context)
    elif data == "settings":
        await query.edit_message_text(
            "Settings:", reply_markup=settings_keyboard()
        )
    elif data == "subscribe":
        await query.edit_message_text("आप अब सब्सक्राइब्ड हैं ✅\nBack to menu:", reply_markup=main_menu_keyboard())
    elif data == "unsubscribe":
        await query.edit_message_text("आपने अनसब्सक्राइब कर दिया है 🔕\nBack to menu:", reply_markup=main_menu_keyboard())
    elif data == "back_main":
        await query.edit_message_text("मुख्य मेनू:", reply_markup=main_menu_keyboard())
    else:
        await query.edit_message_text("Unknown action. Try /help")


# Contact conversation helpers
async def start_contact_flow(query_or_update, context: ContextTypes.DEFAULT_TYPE):
    """
    Start the contact flow. This helper supports being called from callback query or command.
    We'll store the initiating chat id in context when needed.
    """
    # If called from CallbackQuery, we already handled the edit_message_text in callback_router
    # Here we just return the first state to the ConversationHandler (if integrated).
    return CONTACT_NAME


async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await is_banned(update.effective_user.id):
        await update.message.reply_text("You are banned and cannot contact.")
        return ConversationHandler.END

    await update.message.reply_text("कृपया अपना नाम भेजें (या /cancel):")
    return CONTACT_NAME


async def contact_name_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    context.user_data["contact_name"] = name
    await update.message.reply_text("अपना संदेश भेजें:")
    return CONTACT_MESSAGE


async def contact_message_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user = update.effective_user
    # Log message
    await log_message(user.id, text)
    # Send to admin
    admin_text = (
        f"📨 New contact message\n"
        f"From: {user.mention_html()}\n"
        f"Name: {context.user_data.get('contact_name')}\n"
        f"Message: {text}\n"
        f"At: {datetime.utcnow().isoformat()}Z"
    )
    try:
        if ADMIN_ID:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.exception("Failed to notify admin: %s", e)
    await update.message.reply_text("धन्यवाद! हमने आपका संदेश प्राप्त कर लिया है।")
    context.user_data.pop("contact_name", None)
    return ConversationHandler.END


async def contact_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Contact cancelled.")
    context.user_data.pop("contact_name", None)
    return ConversationHandler.END


# Admin commands
async def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid != ADMIN_ID:
            await update.effective_message.reply_text("This command is for admin only.")
            return
        return await func(update, context)
    return wrapper


@admin_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        users_count = (await cur.fetchone())[0]
        cur = await db.execute("SELECT COUNT(*) FROM messages")
        messages_count = (await cur.fetchone())[0]
    await update.effective_message.reply_text(f"Users: {users_count}\nMessages logged: {messages_count}")


@admin_only
async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Usage: /broadcast Your message here
    args = context.args
    if not args:
        await update.effective_message.reply_text("Usage: /broadcast <message>")
        return
    text = " ".join(args)
    # fetch users
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
    if not rows:
        await update.effective_message.reply_text("No users to broadcast.")
        return
    sent = 0
    failed = 0
    for (user_id,) in rows:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            sent += 1
            await asyncio.sleep(0.05)  # small throttle
        except Exception:
            failed += 1
            logger.exception("Broadcast to %s failed", user_id)
    await update.effective_message.reply_text(f"Broadcast complete. Sent: {sent}, Failed: {failed}")


@admin_only
async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Usage: /ban <user_id> [reason]
    args = context.args
    if not args:
        await update.effective_message.reply_text("Usage: /ban <user_id> [reason]")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid user_id.")
        return
    reason = " ".join(args[1:]) if len(args) > 1 else ""
    await ban_user(user_id, reason)
    await update.effective_message.reply_text(f"Banned {user_id}. Reason: {reason}")


@admin_only
async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Usage: /unban <user_id>
    args = context.args
    if not args:
        await update.effective_message.reply_text("Usage: /unban <user_id>")
        return
    try:
        user_id = int(args[0])
    except ValueError:
        await update.effective_message.reply_text("Invalid user_id.")
        return
    await unban_user(user_id)
    await update.effective_message.reply_text(f"Unbanned {user_id}.")


# Generic message handler (rate-limiting & logging)
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if await is_banned(user.id):
        # ignore messages from banned users
        return
    if is_rate_limited(user.id):
        await update.message.reply_text("You're sending messages too fast. Please slow down.")
        return
    # log text (if any)
    if update.message.text:
        await log_message(user.id, update.message.text)
    # simple echo/helpful reply
    await update.message.reply_text(
        "मैं आपकी मदद के लिए यहाँ हूँ — /help टाइप करें या मेनू खोलने के लिए /start दबाएँ."
    )


# Error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Error while handling an update: %s", context.error)
    # Notify admin
    try:
        if ADMIN_ID:
            text = (
                f"⚠️ <b>Exception</b>\n"
                f"{context.error}\n"
                f"Update: {update}"
            )
            await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        logger.exception("Failed to notify admin about error.")


# Startup / Shutdown
async def on_startup(app):
    logger.info("Bot starting up...")
    await init_db()


async def on_shutdown(app):
    logger.info("Bot shutting down...")


def build_app():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Basic commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("about", about_cmd))
    application.add_handler(CommandHandler("contact", contact_command))

    # Admin commands
    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("ban", cmd_ban))
    application.add_handler(CommandHandler("unban", cmd_unban))

    # Callback queries (menus)
    application.add_handler(CallbackQueryHandler(callback_router))

    # Conversation for contact
    conv = ConversationHandler(
        entry_points=[CommandHandler("contact", contact_command)],
        states={
            CONTACT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_name_received)],
            CONTACT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, contact_message_received)],
        },
        fallbacks=[CommandHandler("cancel", contact_cancel)],
        name="contact_conv",
        persistent=False,
    )
    application.add_handler(conv)

    # Generic message handler
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, message_handler))

    # Errors
    application.add_error_handler(error_handler)

    return application


async def main():
    app = build_app()
    app.post_init = on_startup
    app.stop_signals = ( )
    # start
    await app.initialize()
    await app.start()
    logger.info("Bot started — polling.")
    # Use polling for simplicity. For production you may want webhooks.
    await app.updater.start_polling()
    # idle waits until signal; since we don't rely on signals here, use idle()
    await app.updater.idle()
    # shutdown
    await app.stop()
    await app.shutdown()
    logger.info("Bot stopped.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Exiting...")
