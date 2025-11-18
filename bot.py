# bot.py — Feature-rich, PTB v20+ compatible Image->PDF bot (cleaned)
import os
import tempfile
from pathlib import Path
import img2pdf
import json
import time
import hashlib
import asyncio
import uuid
import logging
import requests
import shutil
from urllib.parse import urlparse

from PIL import Image

from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

# ---------- Configuration ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# NOTE: Verify this admin ID list. I kept the value you provided.
ADMINS = [6047187036]  # numeric Telegram user IDs allowed to admin operations
FILES_JSON = "files.json"
MAX_IMAGES = 25  # per-session
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_DIMENSION_PX = 4000  # if image dimension (w or h) > this, we'll resize down
DEFAULT_PAGE_SIZE = "AUTO"  # AUTO | A4 | LETTER
DEFAULT_MARGIN_MM = 0

# ---------- persistence helpers ----------
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
        "time": int(time.time()),
    }
    await save_index(index)
    return slug


# ---------- helpers ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


def readable_size(n):
    # returns human-friendly size
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def ensure_image_dimensions(path: str):
    """
    Open image at path, if dimensions exceed MAX_DIMENSION_PX then resize proportionally.
    Overwrites file at path if resized.
    Returns True if OK, False if can't open.
    """
    try:
        with Image.open(path) as im:
            w, h = im.size
            if max(w, h) <= MAX_DIMENSION_PX:
                return True
            # resize proportionally
            ratio = MAX_DIMENSION_PX / max(w, h)
            new_size = (int(w * ratio), int(h * ratio))
            im = im.convert("RGB")
            im = im.resize(new_size, Image.LANCZOS)
            im.save(path, format="JPEG", quality=85)
            logger.info("Resized image %s -> %s", path, new_size)
            return True
    except Exception as e:
        logger.exception("Failed to process image %s: %s", path, e)
        return False


# ---------- bot handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # initialize per-user pdf settings if not exists
    context.user_data.setdefault(
        "pdf_options", {"page_size": DEFAULT_PAGE_SIZE, "margin_mm": DEFAULT_MARGIN_MM}
    )
    # don't wipe pdf_options when clearing session, keep it consistent
    if "photos" in context.user_data:
        # preserve options, clear only photos
        opts = context.user_data.get("pdf_options", {})
        context.user_data.clear()
        context.user_data["pdf_options"] = opts
    else:
        context.user_data.setdefault(
            "pdf_options", {"page_size": DEFAULT_PAGE_SIZE, "margin_mm": DEFAULT_MARGIN_MM}
        )

    await update.message.reply_text(
        "Namaste 👋\nMain aapka Image→PDF bot hoon.\n\n"
        "Use karne ka tarika:\n"
        "1) Ek ya zyada images bhejo\n"
        "2) Har image ke baad main puchunga — 'Aur bhejni hai' ya 'PDF bana do'\n"
        "3) 'PDF bana do' dabate hi main PDF bana kar bhej dunga\n\n"
        "Commands:\n"
        "/start  /convert  /cancel  /max\n"
        "/find <text>  /get <slug>\n"
        "/pagesize <AUTO|A4|LETTER>  /margin <mm>\n"
        "/list  /myuploads  /rename <slug> <new title>  /delete <slug> (admin)"
    )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    # safety: check file_size if available
    photo = update.message.photo[-1]
    file_size = getattr(photo, "file_size", None)
    if file_size and file_size > MAX_FILE_SIZE_BYTES:
        await update.message.reply_text(
            f"Image bahut badi hai ({readable_size(file_size)}). Max allowed {readable_size(MAX_FILE_SIZE_BYTES)}."
        )
        return

    photos = context.user_data.get("photos", [])
    if len(photos) >= MAX_IMAGES:
        await update.message.reply_text(f"Max limit reached ({MAX_IMAGES}).")
        return

    photos.append(photo.file_id)
    context.user_data["photos"] = photos

    keyboard = [
        [InlineKeyboardButton("Aur bhejni hai", callback_data="add_more")],
        [InlineKeyboardButton("PDF bana do", callback_data="convert")],
        [InlineKeyboardButton("Dekho settings", callback_data="show_settings")],
    ]
    await update.message.reply_text(
        f"Image received ✅ (Total: {len(photos)})",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data

    if data == "add_more":
        await query.edit_message_text("Theek hai — aur images bhejo.")
        return

    if data == "convert":
        await query.edit_message_text("PDF ban raha hai — thoda intezaar karein...")
        await convert_to_pdf(query.message, context)
        return

    if data == "show_settings":
        opts = context.user_data.get(
            "pdf_options", {"page_size": DEFAULT_PAGE_SIZE, "margin_mm": DEFAULT_MARGIN_MM}
        )
        kb = [
            [
                InlineKeyboardButton("PageSize: A4", callback_data="set_pagesize_A4"),
                InlineKeyboardButton("PageSize: LETTER", callback_data="set_pagesize_LETTER"),
            ],
            [
                InlineKeyboardButton("PageSize: AUTO", callback_data="set_pagesize_AUTO"),
                InlineKeyboardButton("Margin +1mm", callback_data="inc_margin"),
                InlineKeyboardButton("Margin -1mm", callback_data="dec_margin"),
            ],
        ]
        await query.edit_message_text(
            f"Current settings: page_size={opts['page_size']}, margin={opts['margin_mm']}mm",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    if data.startswith("set_pagesize_"):
        chosen = data[len("set_pagesize_"):]
        context.user_data.setdefault("pdf_options", {})["page_size"] = chosen
        await query.edit_message_text(f"Page size set to {chosen}.")
        return

    if data == "inc_margin":
        opts = context.user_data.setdefault("pdf_options", {})
        opts["margin_mm"] = max(0, opts.get("margin_mm", DEFAULT_MARGIN_MM) + 1)
        await query.edit_message_text(f"Margin set to {opts['margin_mm']} mm.")
        return

    if data == "dec_margin":
        opts = context.user_data.setdefault("pdf_options", {})
        opts["margin_mm"] = max(0, opts.get("margin_mm", DEFAULT_MARGIN_MM) - 1)
        await query.edit_message_text(f"Margin set to {opts['margin_mm']} mm.")
        return


async def convert_to_pdf(message, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if not photos:
        await message.reply_text("Koi images nahi mili. Pehle images bhejo.")
        return

    # get user pdf options (session)
    pdf_opts = context.user_data.get("pdf_options", {"page_size": DEFAULT_PAGE_SIZE, "margin_mm": DEFAULT_MARGIN_MM})
    page_size = pdf_opts.get("page_size", DEFAULT_PAGE_SIZE)
    margin_mm = int(pdf_opts.get("margin_mm", DEFAULT_MARGIN_MM))

    tmpdir = tempfile.mkdtemp(prefix="img2pdf_")
    paths = []
    try:
        # Download all images with pre-checks
        for i, file_id in enumerate(photos, start=1):
            file = await context.bot.get_file(file_id)
            # check file_size if available again
            if getattr(file, "file_size", None) and file.file_size > MAX_FILE_SIZE_BYTES:
                await message.reply_text(
                    f"Image #{i} bahut badi hai ({readable_size(file.file_size)}). Max allowed {readable_size(MAX_FILE_SIZE_BYTES)}."
                )
                return
            dest = Path(tmpdir) / f"img_{i}.jpg"
            await file.download_to_drive(custom_path=str(dest))
            # after download check actual size on disk
            st = dest.stat()
            if st.st_size > MAX_FILE_SIZE_BYTES:
                await message.reply_text(f"Downloaded image #{i} bahut badi hai ({readable_size(st.st_size)}).")
                return
            # ensure dims
            ok = ensure_image_dimensions(str(dest))
            if not ok:
                await message.reply_text(f"Image #{i} ko process nahi kar paya. Try a different image.")
                return
            paths.append(str(dest))

        # Build img2pdf options
        pagesize_arg = None
        if page_size == "A4":
            pagesize_arg = img2pdf.mm_to_pt((210, 297))
        elif page_size == "LETTER":
            pagesize_arg = img2pdf.mm_to_pt((216, 279))
        pdf_path = Path(tmpdir) / "images_converted.pdf"
        if pagesize_arg:
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(paths, pagesize=pagesize_arg))
        else:
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(paths))

        # Send the PDF
        sent = await message.reply_document(document=InputFile(str(pdf_path)), filename="images_converted.pdf")

        # Index the sent file (store Telegram's server file_id)
        try:
            sent_file_id = None
            if sent and getattr(sent, "document", None):
                sent_file_id = sent.document.file_id
            if sent_file_id:
                title = " / ".join([str(p) for p in photos])[:128]
                uploader_id = getattr(message.from_user, "id", None)
                slug = await index_pdf(context, title, sent_file_id, uploader_id)
                await message.reply_text(f"PDF stored with slug: {slug}\nUse /get {slug} to retrieve.")
        except Exception as e:
            logger.warning("Indexing failed: %s", e)

        context.user_data.clear()

    except Exception as e:
        logger.exception("Error while converting to pdf: %s", e)
        await message.reply_text("Error while creating PDF. Try again later.")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


# ----- simple commands -----
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


# ------------- index / search / admin commands --------------
async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /find <search text>")
        return
    q = " ".join(args).lower()
    index = context.bot_data.get("files", {}) or {}
    results = []
    for slug, meta in index.items():
        if q in meta.get("title", "").lower() or q in slug:
            results.append((slug, meta))
    if not results:
        await update.message.reply_text("Kuch nahi mila.")
        return
    text = "Search results:\n" + "\n".join([f"{s} — {m.get('title')}" for s, m in results[:50]])
    await update.message.reply_text(text)


async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /get <slug>")
        return
    slug = args[0].strip()
    index = context.bot_data.get("files", {}) or {}
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


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    index = context.bot_data.get("files", {}) or {}
    items = sorted(index.items(), key=lambda kv: kv[1].get("time", 0), reverse=True)[:50]
    if not items:
        await update.message.reply_text("Koi stored PDFs nahi hain.")
        return
    text = "Recent PDFs:\n" + "\n".join([f"{slug} — {meta.get('title')}" for slug, meta in items])
    await update.message.reply_text(text)


async def myuploads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    index = context.bot_data.get("files", {}) or {}
    items = [(s, m) for s, m in index.items() if m.get("uploader") == uid]
    if not items:
        await update.message.reply_text("Aapne koi uploads nahi kiye.")
        return
    text = "Aapke uploads:\n" + "\n".join([f"{s} — {m.get('title')}" for s, m in items])
    await update.message.reply_text(text)


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /delete <slug> (admin only)")
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Sirf admin kar sakta hai.")
        return
    slug = args[0].strip()
    index = context.bot_data.get("files", {}) or {}
    if slug not in index:
        await update.message.reply_text("Slug nahi mila.")
        return
    del index[slug]
    await save_index(index)
    await update.message.reply_text(f"Deleted {slug} from index.")


async def rename_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /rename <slug> <new title>")
        return
    slug = args[0].strip()
    new_title = " ".join(args[1:]).strip()
    index = context.bot_data.get("files", {}) or {}
    meta = index.get(slug)
    if not meta:
        await update.message.reply_text("Slug nahi mila.")
        return
    user_id = update.effective_user.id
    if meta.get("uploader") != user_id and not is_admin(user_id):
        await update.message.reply_text("Sirf uploader ya admin rename kar sakta hai.")
        return
    meta["title"] = new_title
    index[slug] = meta
    await save_index(index)
    await update.message.reply_text(f"{slug} ka title ab '{new_title}' ho gaya.")


# PDF option commands
async def pagesize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /pagesize <AUTO|A4|LETTER>")
        return
    val = args[0].upper()
    if val not in ("AUTO", "A4", "LETTER"):
        await update.message.reply_text("Invalid. Use AUTO, A4, or LETTER.")
        return
    context.user_data.setdefault("pdf_options", {})["page_size"] = val
    await update.message.reply_text(f"Page size set to {val} for this session.")


async def margin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /margin <millimeters>")
        return
    try:
        mm = max(0, int(args[0]))
    except Exception:
        await update.message.reply_text("Please provide integer mm.")
        return
    context.user_data.setdefault("pdf_options", {})["margin_mm"] = mm
    await update.message.reply_text(f"Margin set to {mm} mm for this session.")


# ------------- inline query handler --------------
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = (iq.query or "").strip().lower()
    index = context.bot_data.get("files", {}) or {}
    results = []

    items = list(index.items())
    items.sort(key=lambda kv: kv[1].get("time", 0), reverse=True)

    # If there is query, filter; else show recent
    for slug, meta in items[:50]:
        title = meta.get("title") or slug
        if q and q not in slug and q not in title.lower():
            continue
        content = InputTextMessageContent(f"PDF: {title}\nUse /get {slug} to retrieve.")
        results.append(
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title=title[:64],
                input_message_content=content,
                description=slug,
            )
        )
    try:
        await iq.answer(results[:50], cache_time=0)
    except Exception as e:
        logger.exception("Failed to answer inline query: %s", e)


# debug
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
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("myuploads", myuploads_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("rename", rename_cmd))
    app.add_handler(CommandHandler("pagesize", pagesize_cmd))
    app.add_handler(CommandHandler("margin", margin_cmd))

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
        webhook_url = os.environ.get("WEBHOOK_URL")
        port = int(os.environ.get("PORT", "8443"))
        if not webhook_url:
            print("WEBHOOK_URL missing while USE_WEBHOOK=1")
            return
        parsed = urlparse(webhook_url)
        url_path = parsed.path.lstrip("/")
        secret = os.environ.get("TELEGRAM_SECRET")
        set_hook_payload = {"url": webhook_url}
        if secret:
            set_hook_payload["secret_token"] = secret
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/setWebhook", data=set_hook_payload, timeout=10)
            logger.info("setWebhook response: %s", r.text)
        except Exception as e:
            logger.warning("Failed to set webhook via API: %s", e)

        logger.info("Starting webhook mode — listening on port %s, url_path=%s", port, url_path)
        app.run_webhook(listen="0.0.0.0", port=port, webhook_url=webhook_url, url_path=url_path)
    else:
        logger.info("Starting in polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ADMINS = [6047187036]  # numeric Telegram user IDs allowed to admin operations
FILES_JSON = "files.json"
MAX_IMAGES = 25  # per-session
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_DIMENSION_PX = 4000  # if image dimension (w or h) > this, we'll resize down
DEFAULT_PAGE_SIZE = "AUTO"  # AUTO | A4 | LETTER
DEFAULT_MARGIN_MM = 0

# ---------- persistence helpers ----------
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
        "time": int(time.time()),
    }
    await save_index(index)
    return slug

# ---------- helpers ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

def readable_size(n):
    # returns human-friendly size
    for unit in ("B","KB","MB","GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"

def ensure_image_dimensions(path: str):
    """
    Open image at path, if dimensions exceed MAX_DIMENSION_PX then resize proportionally.
    Overwrites file at path if resized.
    Returns True if OK, False if can't open.
    """
    try:
        with Image.open(path) as im:
            w, h = im.size
            if max(w, h) <= MAX_DIMENSION_PX:
                return True
            # resize proportionally
            ratio = MAX_DIMENSION_PX / max(w, h)
            new_size = (int(w * ratio), int(h * ratio))
            im = im.convert("RGB")
            im = im.resize(new_size, Image.LANCZOS)
            im.save(path, format="JPEG", quality=85)
            logger.info("Resized image %s -> %s", path, new_size)
            return True
    except Exception as e:
        logger.exception("Failed to process image %s: %s", path, e)
        return False

def img2pdf_layout_args(page_size: str, margin_mm: int):
    """
    Returns img2pdf options dict for page size & margin.
    page_size: AUTO | A4 | LETTER
    margin_mm: integer (mm)
    """
    # img2pdf takes layout_fun arguments as 'layout_fun' or 'x' options.
    # We'll build a simple 'layout' using img2pdf.get_pdf_bytes parameters via 'with open' usage below.
    # Simpler approach: build a list of pagespec for each image passing layout via "dpi" and "x" not needed.
    # For A4 and LETTER we can use standardized sizes in points (1pt = 1/72 in). img2pdf expects "pt" units via e.g. "A4".
    # We'll use 'pagesize' parameter in convert via 'img2pdf.convert(files, pagesize=...)' by passing tuple (width, height) in points or keywords.
    if page_size == "A4":
        return {"pagesize": img2pdf.mm_to_pt((210, 297)), "x": None}
    if page_size == "LETTER":
        return {"pagesize": img2pdf.mm_to_pt((216, 279)), "x": None}
    return {"pagesize": None, "x": None}

# ---------- bot handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # initialize per-user pdf settings if not exists
    context.user_data.setdefault("pdf_options", {"page_size": DEFAULT_PAGE_SIZE, "margin_mm": DEFAULT_MARGIN_MM})
    context.user_data.clear()
    context.user_data.setdefault("pdf_options", {"page_size": DEFAULT_PAGE_SIZE, "margin_mm": DEFAULT_MARGIN_MM})
    await update.message.reply_text(
        "Namaste 👋\nMain aapka Image→PDF bot hoon.\n\n"
        "Use karne ka tarika:\n"
        "1) Ek ya zyada images bhejo\n"
        "2) Har image ke baad main puchunga — 'Aur bhejni hai' ya 'PDF bana do'\n"
        "3) 'PDF bana do' dabate hi main PDF bana kar bhej dunga\n\n"
        "Commands:\n"
        "/start  /convert  /cancel  /max\n"
        "/find <text>  /get <slug>\n"
        "/pagesize <AUTO|A4|LETTER>  /margin <mm>\n"
        "/list  /myuploads  /rename <slug> <new title>  /delete <slug> (admin)"
    )

async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    # safety: check file_size if available
    photo = update.message.photo[-1]
    file_size = getattr(photo, "file_size", None)
    if file_size and file_size > MAX_FILE_SIZE_BYTES:
        await update.message.reply_text(f"Image bahut badi hai ({readable_size(file_size)}). Max allowed {readable_size(MAX_FILE_SIZE_BYTES)}.")
        return

    photos = context.user_data.get("photos", [])
    if len(photos) >= MAX_IMAGES:
        await update.message.reply_text(f"Max limit reached ({MAX_IMAGES}).")
        return

    photos.append(photo.file_id)
    context.user_data["photos"] = photos

    keyboard = [
        [InlineKeyboardButton("Aur bhejni hai", callback_data="add_more")],
        [InlineKeyboardButton("PDF bana do", callback_data="convert")],
        [InlineKeyboardButton("Dekho settings", callback_data="show_settings")],
    ]
    await update.message.reply_text(
        f"Image received ✅ (Total: {len(photos)})",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    data = query.data

    if data == "add_more":
        await query.edit_message_text("Theek hai — aur images bhejo.")
        return

    if data == "convert":
        await query.edit_message_text("PDF ban raha hai — thoda intezaar karein...")
        await convert_to_pdf(query.message, context)
        return

    if data == "show_settings":
        opts = context.user_data.get("pdf_options", {"page_size": DEFAULT_PAGE_SIZE, "margin_mm": DEFAULT_MARGIN_MM})
        kb = [
            [InlineKeyboardButton("PageSize: A4", callback_data="set_pagesize_A4"),
             InlineKeyboardButton("PageSize: LETTER", callback_data="set_pagesize_LETTER")],
            [InlineKeyboardButton("PageSize: AUTO", callback_data="set_pagesize_AUTO"),
             InlineKeyboardButton("Margin +1mm", callback_data="inc_margin"),
             InlineKeyboardButton("Margin -1mm", callback_data="dec_margin")],
        ]
        await query.edit_message_text(f"Current settings: page_size={opts['page_size']}, margin={opts['margin_mm']}mm", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("set_pagesize_"):
        chosen = data[len("set_pagesize_"):]
        context.user_data.setdefault("pdf_options", {})["page_size"] = chosen
        await query.edit_message_text(f"Page size set to {chosen}.")
        return

    if data == "inc_margin":
        opts = context.user_data.setdefault("pdf_options", {})
        opts["margin_mm"] = max(0, opts.get("margin_mm", DEFAULT_MARGIN_MM) + 1)
        await query.edit_message_text(f"Margin set to {opts['margin_mm']} mm.")
        return

    if data == "dec_margin":
        opts = context.user_data.setdefault("pdf_options", {})
        opts["margin_mm"] = max(0, opts.get("margin_mm", DEFAULT_MARGIN_MM) - 1)
        await query.edit_message_text(f"Margin set to {opts['margin_mm']} mm.")
        return

async def convert_to_pdf(message, context: ContextTypes.DEFAULT_TYPE):
    photos = context.user_data.get("photos", [])
    if not photos:
        await message.reply_text("Koi images nahi mili. Pehle images bhejo.")
        return

    # get user pdf options (session)
    pdf_opts = context.user_data.get("pdf_options", {"page_size": DEFAULT_PAGE_SIZE, "margin_mm": DEFAULT_MARGIN_MM})
    page_size = pdf_opts.get("page_size", DEFAULT_PAGE_SIZE)
    margin_mm = int(pdf_opts.get("margin_mm", DEFAULT_MARGIN_MM))

    tmpdir = tempfile.mkdtemp(prefix="img2pdf_")
    paths = []
    try:
        # Download all images with pre-checks
        for i, file_id in enumerate(photos, start=1):
            file = await context.bot.get_file(file_id)
            # check file_size if available again
            if getattr(file, "file_size", None) and file.file_size > MAX_FILE_SIZE_BYTES:
                await message.reply_text(f"Image #{i} bahut badi hai ({readable_size(file.file_size)}). Max allowed {readable_size(MAX_FILE_SIZE_BYTES)}.")
                return
            dest = Path(tmpdir) / f"img_{i}.jpg"
            await file.download_to_drive(custom_path=str(dest))
            # after download check actual size on disk
            st = dest.stat()
            if st.st_size > MAX_FILE_SIZE_BYTES:
                await message.reply_text(f"Downloaded image #{i} bahut badi hai ({readable_size(st.st_size)}).")
                return
            # ensure dims
            ok = ensure_image_dimensions(str(dest))
            if not ok:
                await message.reply_text(f"Image #{i} ko process nahi kar paya. Try a different image.")
                return
            paths.append(str(dest))

        # Build img2pdf options
        pagesize_arg = None
        if page_size == "A4":
            pagesize_arg = img2pdf.mm_to_pt((210, 297))
        elif page_size == "LETTER":
            pagesize_arg = img2pdf.mm_to_pt((216, 279))
        # margin in mm -> tuple left, top, right, bottom in pt
        margin_pt = int(margin_mm) * 2.834645  # 1 mm = 2.834645669 pt
        pdf_bytes = None
        if pagesize_arg:
            # Use convert with pagesize and border (via layout_fun is complex) — instead we will use default fit (AUTO) for simplicity
            with open(Path(tmpdir) / "images_converted.pdf", "wb") as f:
                f.write(img2pdf.convert(paths, pagesize=pagesize_arg))
            pdf_path = Path(tmpdir) / "images_converted.pdf"
        else:
            # AUTO
            pdf_path = Path(tmpdir) / "images_converted.pdf"
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(paths))

        # Send the PDF
        sent = await message.reply_document(document=InputFile(str(pdf_path)), filename="images_converted.pdf")

        # Index the sent file (store Telegram's server file_id)
        try:
            sent_file_id = None
            if sent and getattr(sent, "document", None):
                sent_file_id = sent.document.file_id
            if sent_file_id:
                title = " / ".join([str(p) for p in photos])[:128]
                uploader_id = getattr(message.from_user, "id", None)
                slug = await index_pdf(context, title, sent_file_id, uploader_id)
                await message.reply_text(f"PDF stored with slug: {slug}\nUse /get {slug} to retrieve.")
        except Exception as e:
            logger.warning("Indexing failed: %s", e)

        context.user_data.clear()

    except Exception as e:
        logger.exception("Error while converting to pdf: %s", e)
        await message.reply_text("Error while creating PDF. Try again later.")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

# ----- simple commands -----
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

# ------------- index / search / admin commands --------------
async def find_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /find <search text>")
        return
    q = " ".join(args).lower()
    index = context.bot_data.get("files", {}) or {}
    results = []
    for slug, meta in index.items():
        if q in meta.get("title", "").lower() or q in slug:
            results.append((slug, meta))
    if not results:
        await update.message.reply_text("Kuch nahi mila.")
        return
    text = "Search results:\n" + "\n".join([f"{s} — {m.get('title')}" for s, m in results[:50]])
    await update.message.reply_text(text)

async def get_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /get <slug>")
        return
    slug = args[0].strip()
    index = context.bot_data.get("files", {}) or {}
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

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    index = context.bot_data.get("files", {}) or {}
    items = sorted(index.items(), key=lambda kv: kv[1].get("time", 0), reverse=True)[:50]
    if not items:
        await update.message.reply_text("Koi stored PDFs nahi hain.")
        return
    text = "Recent PDFs:\n" + "\n".join([f"{slug} — {meta.get('title')}" for slug, meta in items])
    await update.message.reply_text(text)

async def myuploads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    index = context.bot_data.get("files", {}) or {}
    items = [(s,m) for s,m in index.items() if m.get("uploader") == uid]
    if not items:
        await update.message.reply_text("Aapne koi uploads nahi kiye.")
        return
    text = "Aapke uploads:\n" + "\n".join([f"{s} — {m.get('title')}" for s,m in items])
    await update.message.reply_text(text)

async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /delete <slug> (admin only)")
        return
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("Sirf admin kar sakta hai.")
        return
    slug = args[0].strip()
    index = context.bot_data.get("files", {}) or {}
    if slug not in index:
        await update.message.reply_text("Slug nahi mila.")
        return
    # optionally attempt to delete from file store — but we only store telegram file_id, can't delete remote
    del index[slug]
    await save_index(index)
    await update.message.reply_text(f"Deleted {slug} from index.")

async def rename_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /rename <slug> <new title>")
        return
    slug = args[0].strip()
    new_title = " ".join(args[1:]).strip()
    index = context.bot_data.get("files", {}) or {}
    meta = index.get(slug)
    if not meta:
        await update.message.reply_text("Slug nahi mila.")
        return
    user_id = update.effective_user.id
    if meta.get("uploader") != user_id and not is_admin(user_id):
        await update.message.reply_text("Sirf uploader ya admin rename kar sakta hai.")
        return
    meta["title"] = new_title
    index[slug] = meta
    await save_index(index)
    await update.message.reply_text(f"{slug} ka title ab '{new_title}' ho gaya.")

# PDF option commands
async def pagesize_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /pagesize <AUTO|A4|LETTER>")
        return
    val = args[0].upper()
    if val not in ("AUTO","A4","LETTER"):
        await update.message.reply_text("Invalid. Use AUTO, A4, or LETTER.")
        return
    context.user_data.setdefault("pdf_options", {})["page_size"] = val
    await update.message.reply_text(f"Page size set to {val} for this session.")

async def margin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /margin <millimeters>")
        return
    try:
        mm = max(0, int(args[0]))
    except Exception:
        await update.message.reply_text("Please provide integer mm.")
        return
    context.user_data.setdefault("pdf_options", {})["margin_mm"] = mm
    await update.message.reply_text(f"Margin set to {mm} mm for this session.")

# ------------- inline query handler --------------
async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    iq = update.inline_query
    q = (iq.query or "").strip().lower()
    index = context.bot_data.get("files", {}) or {}
    results = []

    items = list(index.items())
    items.sort(key=lambda kv: kv[1].get("time", 0), reverse=True)

    # If there is query, filter; else show recent
    for slug, meta in items[:50]:
        title = meta.get("title") or slug
        if q and q not in slug and q not in title.lower():
            continue
        content = InputTextMessageContent(f"PDF: {title}\nUse /get {slug} to retrieve.")
        results.append(
            InlineQueryResultArticle(
                id=str(uuid.uuid4()),
                title=title[:64],
                input_message_content=content,
                description=slug,
            )
        )
    try:
        await iq.answer(results[:50], cache_time=0)
    except Exception as e:
        logger.exception("Failed to answer inline query: %s", e)

# debug
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
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("myuploads", myuploads_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("rename", rename_cmd))
    app.add_handler(CommandHandler("pagesize", pagesize_cmd))
    app.add_handler(CommandHandler("margin", margin_cmd))

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
        webhook_url = os.environ.get("WEBHOOK_URL")
        port = int(os.environ.get("PORT", "8443"))
        if not webhook_url:
            print("WEBHOOK_URL missing while USE_WEBHOOK=1")
            return
        parsed = urlparse(webhook_url)
        url_path = parsed.path.lstrip("/")
        secret = os.environ.get("TELEGRAM_SECRET")
        set_hook_payload = {"url": webhook_url}
        if secret:
            set_hook_payload["secret_token"] = secret
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/setWebhook", data=set_hook_payload, timeout=10)
            logger.info("setWebhook response: %s", r.text)
        except Exception as e:
            logger.warning("Failed to set webhook via API: %s", e)

        logger.info("Starting webhook mode — listening on port %s, url_path=%s", port, url_path)
        app.run_webhook(listen="0.0.0.0", port=port, webhook_url=webhook_url, url_path=url_path)
    else:
        logger.info("Starting in polling mode")
        app.run_polling()

if __name__ == "__main__":
    main()ADMINS = [6047187036]

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
        if q in meta.get("title", "").lower() or q in slug:
            results.append((slug, meta))
    if not results:
        await update.message.reply_text("Kuch nahi mila.")
        return
    text = "Search results:\n" + "\n".join([f"{s} — {m.get('title')}" for s, m in results[:20]])
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
        # send by file_id directly
        await update.message.reply_document(document=file_id, filename=f"{slug}.pdf")
    except Exception as e:
        # fixed: proper logging and reply
        logger.exception("Failed to send file: %s", e)
        await update.message.reply_text("Failed to send file.")        "type": "pdf",
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
