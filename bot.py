#!/usr/bin/env python3
"""
bot.py — Telegram img2pdf bot with optional OCR.

Requirements (example):
pip install python-telegram-bot==20.8 Pillow img2pdf pytesseract PyPDF2 requests

System dependency for OCR:
Install Tesseract binary (e.g., `sudo apt install tesseract-ocr` on Debian/Ubuntu).

Usage:
export TELEGRAM_TOKEN="your_token_here"
python bot.py
"""
import asyncio
import logging
import os
import tempfile
from functools import partial
from typing import Dict, List

from PIL import Image, ImageDraw, ImageFont
import img2pdf

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Optional imports (graceful)
try:
    import pytesseract
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False

try:
    from PyPDF2 import PdfMerger
    PYPDF2_AVAILABLE = True
except Exception:
    PYPDF2_AVAILABLE = False

# --- Configuration ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("Please set TELEGRAM_TOKEN environment variable")

# Limits
MAX_IMAGES_PER_SESSION = 50
MAX_IMAGE_SIZE_MB = 25  # max single upload size to accept/process
MAX_TOTAL_SESSION_SIZE_MB = 200

# In-memory session store (for demo). Use Redis/DB for production persistence.
session_store: Dict[int, Dict] = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Helper functions ---


def ensure_session(chat_id: int) -> Dict:
    if chat_id not in session_store:
        session_store[chat_id] = {
            "images": [],
            "opts": {
                "rotate": 0,
                "compress_scale": 1.0,
                "watermark": None,
                "orientation": "portrait",
                "pagesize": "A4",
                "ocr": False,
                "metadata": {},
            },
        }
    return session_store[chat_id]


def human_readable_size(bytesize: int) -> str:
    if not bytesize:
        return "0B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytesize < 1024.0:
            return f"{bytesize:3.1f}{unit}"
        bytesize /= 1024.0
    return f"{bytesize:.1f}TB"


async def download_telegram_file(file, dest_path: str):
    # file is telegram.File object (async)
    await file.download_to_drive(custom_path=dest_path)


def add_watermark_to_image(img_path: str, text: str) -> str:
    img = Image.open(img_path).convert("RGBA")
    width, height = img.size
    watermark_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(watermark_layer)
    try:
        font = ImageFont.truetype("arial.ttf", max(12, width // 40))
    except Exception:
        font = ImageFont.load_default()
    textwidth, textheight = draw.textsize(text, font=font)
    x = width - textwidth - 10
    y = height - textheight - 10
    draw.text((x, y), text, fill=(255, 255, 255, 128), font=font)
    combined = Image.alpha_composite(img, watermark_layer)
    out_path = img_path + ".wm.png"
    combined.convert("RGB").save(out_path, format="PNG")
    return out_path


def resize_image_if_needed(img_path: str, scale: float) -> str:
    if abs(scale - 1.0) < 1e-6:
        return img_path
    img = Image.open(img_path)
    w, h = img.size
    new_w = max(1, int(w * scale))
    new_h = max(1, int(h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    out_path = img_path + f".resized.png"
    img.save(out_path, format='PNG')
    return out_path


def images_to_pdf(image_paths: List[str], output_path: str, opts: Dict):
    """
    Synchronous conversion. Supports:
      - OCR via pytesseract (per-image PDF bytes) if opts['ocr'] True and pytesseract present
      - Merging OCR PDFs with PyPDF2 PdfMerger if available
      - Fallback to img2pdf for lossless conversion
    """
    # Try OCR path if requested and available
    if opts.get('ocr') and OCR_AVAILABLE:
        temp_pdf_paths = []
        try:
            for img_path in image_paths:
                pil = Image.open(img_path)
                try:
                    pdf_bytes = pytesseract.image_to_pdf_or_hocr(pil, extension='pdf')
                except Exception as e:
                    logger.exception("pytesseract failed on image: %s", e)
                    raise

                fd, tmp_pdf = tempfile.mkstemp(suffix=".pdf")
                os.close(fd)
                with open(tmp_pdf, "wb") as f:
                    f.write(pdf_bytes)
                temp_pdf_paths.append(tmp_pdf)

            if len(temp_pdf_paths) == 1:
                os.replace(temp_pdf_paths[0], output_path)
                return output_path

            if PYPDF2_AVAILABLE:
                merger = PdfMerger()
                for p in temp_pdf_paths:
                    merger.append(p)
                with open(output_path, "wb") as f_out:
                    merger.write(f_out)
                merger.close()
                for p in temp_pdf_paths:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                return output_path
            else:
                logger.warning("PyPDF2 not available — cannot merge OCR PDFs. Falling back to img2pdf.")
                for p in temp_pdf_paths:
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                # fall through to non-OCR path
        except Exception:
            logger.exception("OCR path failed; falling back to img2pdf.")
            for p in temp_pdf_paths:
                try:
                    os.remove(p)
                except Exception:
                    pass

    # Default (non-OCR) path: img2pdf (lossless)
    with open(output_path, "wb") as f_out:
        try:
            f_out.write(img2pdf.convert(image_paths))
        except Exception as e:
            logger.exception("img2pdf failed, falling back to PIL PDF save: %s", e)
            pil_images = []
            for p in image_paths:
                im = Image.open(p)
                if im.mode == 'RGBA':
                    im = im.convert('RGB')
                pil_images.append(im)
            if pil_images:
                pil_images[0].save(output_path, "PDF", resolution=100.0, save_all=True, append_images=pil_images[1:])
    return output_path


# --- Bot handlers ---


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_session(chat_id)
    await update.message.reply_text(
        "Hi! I'm img2pdf — send me images (photos or documents).\n"
        "Commands:\n"
        "/add - start/clear batch\n"
        "/list - show batch count\n"
        "/makepdf - create PDF\n"
        "/options - OCR/watermark/rotate/compress\n"
        "/cancel - clear session\n"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/add - start/clear current image batch\n"
        "/list - show number of images in the batch\n"
        "/makepdf - create PDF from currently stored images\n"
        "/cancel - clear session images\n"
        "/options - configure watermark, rotate, compress, ocr\n"
        "Send multiple images (one-by-one). Then run /makepdf."
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = ensure_session(chat_id)
    for p in session['images']:
        try:
            os.remove(p)
        except Exception:
            pass
    session['images'] = []
    await update.message.reply_text(f"Started a new batch. Send images now (up to {MAX_IMAGES_PER_SESSION}).")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = ensure_session(chat_id)
    count = len(session['images'])
    total = sum(os.path.getsize(p) for p in session['images']) if session['images'] else 0
    await update.message.reply_text(f"You have {count} images in the batch (total {human_readable_size(total)}).")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in session_store:
        for p in session_store[chat_id]['images']:
            try:
                os.remove(p)
            except Exception:
                pass
        session_store.pop(chat_id, None)
    await update.message.reply_text("Session cleared.")


async def options_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_session(chat_id)
    keyboard = [
        [InlineKeyboardButton("Toggle OCR (server: %s)" % ("available" if OCR_AVAILABLE else "not installed"), callback_data="opt_ocr")],
        [InlineKeyboardButton("Set watermark", callback_data="opt_watermark")],
        [InlineKeyboardButton("Rotate 90°", callback_data="opt_rotate")],
        [InlineKeyboardButton("Compress: 75%", callback_data="opt_compress")],
        [InlineKeyboardButton("Make PDF now", callback_data="opt_makepdf")],
    ]
    await update.message.reply_text("Options:", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id
    session = ensure_session(chat_id)
    data = query.data
    if data == 'opt_ocr':
        if not OCR_AVAILABLE:
            await query.edit_message_text("OCR not available on this server (pytesseract/tesseract missing).")
            return
        session['opts']['ocr'] = not session['opts'].get('ocr', False)
        await query.edit_message_text(f"OCR toggled to {session['opts']['ocr']}")
    elif data == 'opt_watermark':
        await query.edit_message_text("Reply to this chat with the watermark text you want to set.")
        context.user_data['expecting_watermark'] = True
    elif data == 'opt_rotate':
        session['opts']['rotate'] = (session['opts'].get('rotate', 0) + 90) % 360
        await query.edit_message_text(f"Rotate set to {session['opts']['rotate']}°")
    elif data == 'opt_compress':
        prev = session['opts'].get('compress_scale', 1.0)
        new = 0.75 if abs(prev - 1.0) < 1e-6 else 1.0
        session['opts']['compress_scale'] = new
        await query.edit_message_text(f"Compress scale set to {new}")
    elif data == 'opt_makepdf':
        await query.edit_message_text('Starting PDF creation...')
        await make_pdf_for_chat(chat_id, context)
    else:
        await query.edit_message_text('Unknown option.')


async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = ensure_session(chat_id)
    if context.user_data.get('expecting_watermark'):
        text = update.message.text.strip()
        session['opts']['watermark'] = text
        context.user_data['expecting_watermark'] = False
        await update.message.reply_text(f"Watermark set to: {text}")
        return
    await update.message.reply_text("I didn't understand. Send images or use /help.")


async def photo_or_document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    session = ensure_session(chat_id)

    file_obj = None
    filename = None
    filesize = 0

    if update.message.photo:
        photo = update.message.photo[-1]
        file_obj = await photo.get_file()
        filename = f"photo_{photo.file_unique_id}.jpg"
        filesize = photo.file_size or 0
    elif update.message.document:
        doc = update.message.document
        if not (doc.mime_type and doc.mime_type.startswith('image')):
            await update.message.reply_text("I can only accept image files as documents. Send images or convert to jpeg/png.")
            return
        file_obj = await doc.get_file()
        filename = doc.file_name or f"doc_{doc.file_unique_id}"
        filesize = doc.file_size or 0
    else:
        await update.message.reply_text("No image found in message.")
        return

    if filesize and filesize > MAX_IMAGE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"File too large ({human_readable_size(filesize)}). Max is {MAX_IMAGE_SIZE_MB}MB")
        return

    if len(session['images']) >= MAX_IMAGES_PER_SESSION:
        await update.message.reply_text(f"You already have {MAX_IMAGES_PER_SESSION} images in the batch. Use /makepdf or /cancel to clear.")
        return

    tmpdir = tempfile.gettempdir()
    out_path = os.path.join(tmpdir, f"img2pdf_{chat_id}_{len(session['images'])}_{filename}")
    try:
        await download_telegram_file(file_obj, out_path)
    except Exception as e:
        logger.exception("Failed to download file: %s", e)
        await update.message.reply_text("Failed to download the image. Try again.")
        return

    session['images'].append(out_path)
    total_size = sum(os.path.getsize(p) for p in session['images'])
    if total_size > MAX_TOTAL_SESSION_SIZE_MB * 1024 * 1024:
        session['images'].pop()
        try:
            os.remove(out_path)
        except Exception:
            pass
        await update.message.reply_text("Total batch size would exceed limit. Try smaller images or fewer files.")
        return

    await update.message.reply_text(f"Saved {filename}. Batch now: {len(session['images'])} images (total {human_readable_size(total_size)})")


async def makepdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await make_pdf_for_chat(chat_id, context)


async def make_pdf_for_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    session = ensure_session(chat_id)
    if not session['images']:
        try:
            await context.bot.send_message(chat_id=chat_id, text="No images in your batch. Send images first or use /add to start fresh.")
        except Exception:
            pass
        return

    # prepare images with options (rotate, watermark, compress)
    work_files = []
    for p in session['images']:
        cur = p
        if session['opts'].get('watermark'):
            try:
                cur = add_watermark_to_image(cur, session['opts']['watermark'])
            except Exception:
                logger.exception("Watermark failed")
        if abs(session['opts'].get('compress_scale', 1.0) - 1.0) > 1e-6:
            try:
                cur = resize_image_if_needed(cur, session['opts']['compress_scale'])
            except Exception:
                logger.exception("Resize failed")
        if session['opts'].get('rotate', 0):
            try:
                im = Image.open(cur)
                im = im.rotate(-session['opts']['rotate'], expand=True)
                outp = cur + f".rot{session['opts']['rotate']}.png"
                im.convert('RGB').save(outp)
                cur = outp
            except Exception:
                logger.exception("Rotate failed")
        work_files.append(cur)

    tmpdir = tempfile.gettempdir()
    out_pdf = os.path.join(tmpdir, f"img2pdf_out_{chat_id}.pdf")
    try:
        await context.bot.send_message(chat_id=chat_id, text="Converting images to PDF...")
        loop = asyncio.get_running_loop()
        # run blocking conversion in thread pool
        await loop.run_in_executor(None, partial(images_to_pdf, work_files, out_pdf, session['opts']))
        # send file
        with open(out_pdf, 'rb') as f:
            await context.bot.send_document(chat_id=chat_id, document=InputFile(f, filename="images.pdf"))
    except Exception as e:
        logger.exception("PDF conversion failed: %s", e)
        await context.bot.send_message(chat_id=chat_id, text=f"Failed to create PDF: {e}")
    finally:
        # keep session by default; user can /cancel to clear
        pass


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Use /help.")


def main():
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CommandHandler('add', add_cmd))
    application.add_handler(CommandHandler('list', list_cmd))
    application.add_handler(CommandHandler('cancel', cancel_cmd))
    application.add_handler(CommandHandler('options', options_cmd))
    application.add_handler(CommandHandler('makepdf', makepdf_cmd))

    application.add_handler(CallbackQueryHandler(handle_callback))

    # handlers for photos and image-documents
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, photo_or_document_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    # unknowns
    application.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))

    logger.info("Starting img2pdf bot")
    application.run_polling()


if __name__ == '__main__':
    main()
