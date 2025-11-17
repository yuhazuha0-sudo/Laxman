import os
import tempfile
from pathlib import Path
import img2pdf
from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("convert", convert_cmd))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("max", max_cmd))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, photo_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_error_handler(error_handler)

    print("Bot running…")
    app.run_polling()

if __name__ == "__main__":
    main()
