# bot.py - minimal Image->PDF bot (python-telegram-bot v20.x)
import os
import tempfile
import shutil
from pathlib import Path
import img2pdf
import logging

from PIL import Image
from telegram import InputFile, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Namaste 👋\nSend an image and I'll convert it to a PDF and send it back.\n\n"
        "Usage: just send a photo (single)."
    )


def ensure_jpeg(path: Path) -> bool:
    """Convert image to JPEG (in-place) so img2pdf accepts it reliably."""
    try:
        with Image.open(path) as im:
            rgb = im.convert("RGB")
            rgb.save(path, "JPEG", quality=85)
        return True
    except Exception:
        logger.exception("Failed to convert image to JPEG: %s", path)
        return False


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # safety checks
    if not update.message or not update.message.photo:
        return

    photo = update.message.photo[-1]

    tmpdir = tempfile.mkdtemp(prefix="bot_img2pdf_")
    try:
        dest = Path(tmpdir) / "image.jpg"
        file_obj = await context.bot.get_file(photo.file_id)
        # download to disk
        await file_obj.download_to_drive(custom_path=str(dest))

        # convert to JPEG for img2pdf reliability
        if not ensure_jpeg(dest):
            await update.message.reply_text("Image process nahi ho payi — ek aur try karo.")
            return

        pdf_path = Path(tmpdir) / "output.pdf"
        # create pdf
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert([str(dest)]))

        # send back to user
        await update.message.reply_document(
            document=InputFile(str(pdf_path)), filename="converted.pdf"
        )

    except Exception:
        logger.exception("Error while converting image to PDF")
        # user-friendly message
        await update.message.reply_text("Kuch gadbad ho gayi — dobara bhejo.")
    finally:
        # cleanup
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Update caused error: %s", context.error)


def main():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("BOT_TOKEN missing in environment — set it and restart.")
        return

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_handler))
    app.add_error_handler(error_handler)

    logger.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
