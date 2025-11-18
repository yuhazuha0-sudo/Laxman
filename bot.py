# bot.py — corrected, PTB v20+ compatible, inline + webhook ready
import os
import tempfile
from pathlib import Path
import img2pdf
from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
import json
import time
import hashlib
import asyncio
import uuid
import logging
import requests
from urllib.parse import urlparse

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Admins (numeric Telegram user IDs)
ADMINS = [6047187036]

FILES_JSON = "files.json"
MAX_IMAGES = 25  # safety limit per session

def load_index():
    try:
        with open(FILES_JSON, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_index_sync(index):
    with open(FILES_JSON, "w") as f:
        json.dump(index, f)

async def save_index(index):
    await asyncio.to_thread(save_index_sync, index)

def make_slug(title: str) -> str:
    s = (title or "file").lower().strip().replace(" ", "_")
    return s + "_" + hashlib.md5((s + str(time.time())).encode()).hexdigest()[:6]

async def index_pdf(context, title: str, file_id: str, uploader_id: int):
    index = context.bot_data.setdefault("files", {})
    slug = make_slug(title)
    index[slug] = {
        "file_id": file_id,
        "title": title,
        "type": "pdf",
        "uploader": uploader_id,
        "time": int(time.time())
    }
    await save_index(index)
    return slug

# ---------------- handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Namaste 👋\nMain aapka Image→PDF bot hoon.\n\n"
        "Use karne ka tarika:\n"
        "1) Ek ya zyada images bhejo\n"
        "2) Har image ke baad main puchunga — 'Aur bhejni hai' ya 'PDF bana do'\n"
        "3) 'PDF bana do' dabate hi main PDF bana kar bhej dunga\n\n"
        "Commands:\n/start  /convert  /cancel  /max\n/find <text>  /get <slug>"
    )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    photos = context.user_data.get("photos", [])
    if len(photos) >= MAX_IMAGES:
        await update.message.reply_text(f"Max limit reached ({MAX_IMAGES}).")
        return

    photo = update.message.photo[-1]
    photos.append(photo.file_id)
    context.user_data["photos"] = photos

    keyboard = [
        [InlineKeyboardButton("Aur bhejni hai", callback_data="add_more")],
        [InlineKeyboardButton("PDF bana do", callback_data="convert")],
    ]
    await update.message.reply_text(
        f"Image received ✅ (Total: {len(photos)})",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add_more":
        await query.edit_message_text("Theek hai — aur images bhejo.")
        return

    if data == "convert":
        await query.edit_message_text("PDF ban raha hai — thoda intezaar karein...")
        await convert_to_pdf(query.message, context)
        return

async def convert_to_pdf(message, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if not photos:
        await message.reply_text("Koi images nahi mili. Pehle images bhejo.")
        return

    tmpdir = tempfile.mkdtemp(prefix="img2pdf_")
    paths = []
    try:
        for i, file_id in enumerate(photos, start=1):
            file = await context.bot.get_file(file_id)
            dest = Path(tmpdir) / f"img_{i}.jpg"
            await file.download_to_drive(custom_path=str(dest))
            paths.append(str(dest))

        pdf_path = Path(tmpdir) / "images_converted.pdf"
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(paths))

        # send and index
        sent = await message.reply_document(
            document=InputFile(open(pdf_path, "rb"), filename="images_converted.pdf")
        )
        try:
            file_id = sent.document.file_id
            title = " / ".join([str(p) for p in photos])[:128]
            slug = await index_pdf(context, title, file_id, message.from_user.id)
            await message.reply_text(f"PDF stored with slug: {slug}\nUse /get {slug} to retrieve.")
        except Exception as e:
            logger.warning("Indexing failed: %s", e)

        context.user_data.clear()

    except Exception as e:
        logger.exception("Error while converting to pdf: %s", e)
        await message.reply_text(f"Error: {e}")
    finally:
        try:
            for p in Path(tmpdir).iterdir():
                p.unlink(missing_ok=True)
            Path(tmpdir).rmdir()
        except Exception:
            pass

async def convert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Converting…")
    await convert_to_pdf(update.message, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Session cancelled.")

async def max_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Max images per session: {MAX_IMAGES}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Handler error: %s", context.error)

# ------------- index / search commands --------------
async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /find <search text>")
        return
    q = " ".join(args).lower()
    index = context.bot_data.get("files", {})
    results = []
    for slug, meta in index.items():
        if q in meta.get("title","").lower() or q in slug:
            results.append((slug, meta))
    if not results:
        await update.message.reply_text("Kuch nahi mila.")
        return
    text = "Search results:\n" + "\n".join([f"{s} — {m.get('title')}" for s,m in results[:20]])
    await update.message.reply_text(text)

async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /get <slug>")
        return
    slug = args[0].strip()
    index = context.bot_data.get("files", {})
    meta = index.get(slug)
    if not meta:
        await update.message.reply_text("Slug nahi mila.")
        return
    try:
        file_id = meta.get("file_id")
        await update.message.reply_document(document=file_id, filename=f"{slug}.pdf")
    except Exception as e:
        logger.exception("Failed to send fileADMINS = [6047187036]

FILES_JSON = "files.json"
MAX_IMAGES = 25  # safety limit per session

def load_index():
    try:
        with open(FILES_JSON, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_index_sync(index):
    with open(FILES_JSON, "w") as f:
        json.dump(index, f)

async def save_index(index):
    await asyncio.to_thread(save_index_sync, index)

def make_slug(title: str) -> str:
    s = (title or "file").lower().strip().replace(" ", "_")
    return s + "_" + hashlib.md5((s + str(time.time())).encode()).hexdigest()[:6]

async def index_pdf(context, title: str, file_id: str, uploader_id: int):
    index = context.bot_data.setdefault("files", {})
    slug = make_slug(title)
    index[slug] = {
        "file_id": file_id,
        "title": title,
        "type": "pdf",
        "uploader": uploader_id,
        "time": int(time.time())
    }
    await save_index(index)
    return slug

# ---------------- handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Namaste 👋\nMain aapka Image→PDF bot hoon.\n\n"
        "Use karne ka tarika:\n"
        "1) Ek ya zyada images bhejo\n"
        "2) Har image ke baad main puchunga — 'Aur bhejni hai' ya 'PDF bana do'\n"
        "3) 'PDF bana do' dabate hi main PDF bana kar bhej dunga\n\n"
        "Commands:\n/start  /convert  /cancel  /max\n/find <text>  /get <slug>"
    )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    photos = context.user_data.get("photos", [])
    if len(photos) >= MAX_IMAGES:
        await update.message.reply_text(f"Max limit reached ({MAX_IMAGES}).")
        return

    photo = update.message.photo[-1]
    photos.append(photo.file_id)
    context.user_data["photos"] = photos

    keyboard = [
        [InlineKeyboardButton("Aur bhejni hai", callback_data="add_more")],
        [InlineKeyboardButton("PDF bana do", callback_data="convert")],
    ]
    await update.message.reply_text(
        f"Image received ✅ (Total: {len(photos)})",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add_more":
        await query.edit_message_text("Theek hai — aur images bhejo.")
        return

    if data == "convert":
        await query.edit_message_text("PDF ban raha hai — thoda intezaar karein...")
        await convert_to_pdf(query.message, context)
        return

async def convert_to_pdf(message, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if not photos:
        await message.reply_text("Koi images nahi mili. Pehle images bhejo.")
        return

    tmpdir = tempfile.mkdtemp(prefix="img2pdf_")
    paths = []
    try:
        for i, file_id in enumerate(photos, start=1):
            file = await context.bot.get_file(file_id)
            dest = Path(tmpdir) / f"img_{i}.jpg"
            await file.download_to_drive(custom_path=str(dest))
            paths.append(str(dest))

        pdf_path = Path(tmpdir) / "images_converted.pdf"
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(paths))

        # send and index
        sent = await message.reply_document(
            document=InputFile(open(pdf_path, "rb"), filename="images_converted.pdf")
        )
        # extract file_id of sent document (telegram returns Message object; server-side file_id in 'document')
        try:
            file_id = sent.document.file_id
            title = " / ".join([str(p) for p in photos])[:128]
            slug = await index_pdf(context, title, file_id, message.from_user.id)
            await message.reply_text(f"PDF stored with slug: {slug}\nUse /get {slug} to retrieve.")
        except Exception as e:
            logger.warning("Indexing failed: %s", e)

        context.user_data.clear()

    except Exception as e:
        logger.exception("Error while converting to pdf: %s", e)
        await message.reply_text(f"Error: {e}")
    finally:
        try:
            for p in Path(tmpdir).iterdir():
                p.unlink(missing_ok=True)
            Path(tmpdir).rmdir()
        except Exception:
            pass

async def convert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Converting…")
    await convert_to_pdf(update.message, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Session cancelled.")

async def max_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Max images per session: {MAX_IMAGES}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Handler error: %s", context.error)

# ------------- index / search commands --------------
async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /find <search text>")
        return
    q = " ".join(args).lower()
    index = context.bot_data.get("files", {})
    results = []
    for slug, meta in index.items():
        if q in meta.get("title","").lower() or q in slug:
            results.append((slug, meta))
    if not results:
        await update.message.reply_text("Kuch nahi mila.")
        return
    text = "Search results:\n" + "\n".join([f"{s} — {m.get('title')}" for s,m in results[:20]])
    await update.message.reply_text(text)

async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /get <slug>")
        return
    slug = args[0].strip()
    index = context.bot_data.get("files", {})
    meta = index.get(slug)
    if not meta:
        await update.message.reply_text("Slug nahi mila.")
        return
    try:
        file_id = meta.get("file_id")
        await update.message.reply_document(document=file_id, filename=f"{slug}.pdf")
    except Exception as e:
        logger.exception("Failed to send file: %s", e)
        await update.message.reply_text("Failed to send file.")

# ------------- inline query handler --------------
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = (iq.query or "").strip().lower()
    index = context.bot_data.get("files", {})
    results = []
    # if no query, show some recent items
    items = list(index.items())
    items.sort(key=lambda kv: kv[1].get("time", 0), reverse=True)
    for slug, meta in items[:10]:
        title = meta.get("title") or slug
        content = InputTextMessageContent(f"PDF: {title}\nUse /get {slug} to retrieve (or click).")
        results.append(
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title=title[:64],
                input_message_content=content,
                description=slug
            )
        )
    # If there is a query, filter by query term
    if q:
        results = []
        for slug, meta in items:
            if q in slug or q in meta.get("title","").lower():
                title = meta.get("title") or slug
                content = InputTextMessageContent(f"PDF: {title}\nUse /get {slug}")
                results.append(
                    InlineQueryResultArticle(
                        id=str(uuid.uuid4()),
                        title=title[:64],
                        input_message_content=content,
                        description=slug
                    )
                )
    # Return results (empty allowed)
    try:
        await iq.answer(results[:50], cache_time=0)
    except Exception as e:
        logger.exception("Failed to answer inline query: %s", e)

# ---------- debug helper (optional) ----------
async def _debug_all_updates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("RECEIVED UPDATE: %s", update)

# ----------------- main ---------------------
def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("BOT_TOKEN missing in environment — set it and restart.")
        return

    app = ApplicationBuilder().token(token).build()
    # preload index into bot_data
    app.bot_data["files"] = load_index()

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("max", max_cmd))
    app.add_handler(CommandHandler("find", find_cmd))
    app.add_handler(CommandHandler("get", get_cmd))

    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(InlineQueryHandler(inline_query_handler))

    app.add_error_handler(error_handler)

    # debug: log all updates if env DEBUG_ALL set
    if os.environ.get("DEBUG_ALL"):
        app.add_handler(MessageHandler(filters.ALL, _debug_all_updates))

    # Choose mode: webhook or polling
    use_webhook = bool(os.environ.get("USE_WEBHOOK"))
    if use_webhook:
        # Webhook mode
        webhook_url = os.environ.get("WEBHOOK_URL")  # full https://your-domain/path
        port = int(os.environ.get("PORT", "8443"))
        if not webhook_url:
            print("WEBHOOK_URL missing while USE_WEBHOOK=1")
            return

        parsed = urlparse(webhook_url)
        url_path = parsed.path.lstrip("/")
        # Optionally verify TELEGRAM_SECRET
        secret = os.environ.get("TELEGRAM_SECRET")
        # set webhook via API (so Telegram knows and includes secret_token header)
        set_hook_payload = {"url": webhook_url}
        if secret:
            set_hook_payload["secret_token"] = secret
        r = requests.post(f"https://api.telegram.org/bot{token}/setWebhook", data=set_hook_payload, timeout=10)
        logger.info("setWebhook response: %s", r.text)

        logger.info("Starting webhook mode — listening on port %s, url_path=%s", port, url_path)
        # PTB's run_webhook will expose HTTP endpoint at /<url_path>
        app.run_webhook(listen="0.0.0.0", port=port, webhook_url=webhook_url, url_path=url_path)
    else:
        # polling (default)
        logger.info("Starting in polling mode")
        app.run_polling()

if __name__ == "__main__":
    main()
def make_slug(title: str) -> str:
    s = (title or "file").lower().strip().replace(" ", "_")
    return s + "_" + hashlib.md5((s + str(time.time())).encode()).hexdigest()[:6]

async def index_pdf(context, title: str, file_id: str, uploader_id: int):
    index = context.bot_data.setdefault("files", {})
    slug = make_slug(title)
    index[slug] = {
        "file_id": file_id,
        "title": title,
        "type": "pdf",
        "uploader": uploader_id,
        "time": int(time.time())
    }
    await save_index(index)
    return slug
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

MAX_IMAGES = 25  # safety limit per session

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Namaste 👋\nMain aapki Image→PDF bot hoon.\n\n"
        "Use karne ka tarika:\n"
        "1) Ek ya zyada images bhejo\n"
        "2) Har image ke baad main puchunga — 'Aur bhejni hai' ya 'PDF bana do'\n"
        "3) 'PDF bana do' dabate hi main PDF bana kar bhej dunga\n\n"
        "Commands:\n/start  /convert  /cancel  /max"
    )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    photos = context.user_data.get("photos", [])
    if len(photos) >= MAX_IMAGES:
        await update.message.reply_text(f"Max limit reached ({MAX_IMAGES}).")
        return

    photo = update.message.photo[-1]
    photos.append(photo.file_id)
    context.user_data["photos"] = photos

    keyboard = [
        [InlineKeyboardButton("Aur bhejni hai", callback_data="add_more")],
        [InlineKeyboardButton("PDF bana do", callback_data="convert")],
    ]
    await update.message.reply_text(
        f"Image received ✅ (Total: {len(photos)})",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "add_more":
        await query.edit_message_text("Theek hai — aur images bhejo.")
        return

    if data == "convert":
        await query.edit_message_text("PDF ban raha hai — wait karein...")
        await convert_to_pdf(query.message, context)
        return

async def convert_to_pdf(message, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if not photos:
        await message.reply_text("Koi images nahi mili. Pehle images bhejo.")
        return

    tmpdir = tempfile.mkdtemp(prefix="img2pdf_")
    paths = []
    try:
        for i, file_id in enumerate(photos, start=1):
            file = await context.bot.get_file(file_id)
            dest = Path(tmpdir) / f"img_{i}.jpg"
            await file.download_to_drive(custom_path=str(dest))
            paths.append(str(dest))

        pdf_path = Path(tmpdir) / "images_converted.pdf"
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(paths))

        await message.reply_document(
            document=InputFile(open(pdf_path, "rb"), filename="images_converted.pdf")
        )
        context.user_data.clear()

    except Exception as e:
        await message.reply_text(f"Error: {e}")
    finally:
        try:
            for p in Path(tmpdir).iterdir():
                p.unlink(missing_ok=True)
            Path(tmpdir).rmdir()
        except:
            pass

async def convert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Converting…")
    await convert_to_pdf(update.message, context)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Session cancelled.")

async def max_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Max images per session: {MAX_IMAGES}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print("Error:", context.error)

def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("BOT_TOKEN missing!")
        return

    app = ApplicationBuilder().token(token).build()
    app.bot_data["files"] = load_index()


    app.bot_data["files"] = load_index()
app.add_handler(CommandHandler("find", find_cmd))
app.add_handler(CommandHandler("get", get_cmd))
app.add_handler(InlineQueryHandler(inline_query_handler))
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("max", max_cmd))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)
from telegram.ext import MessageHandler

async def _debug_all_updates(update, context):
    logger.info("RECEIVED UPDATE: %s", update)

# add this temporarily
app.add_handler(MessageHandler(filters.ALL, _debug_all_updates))

    print("Bot running…")
    app.run_polling()

if __name__ == "__main__":
    main()
