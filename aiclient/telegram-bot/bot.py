import os
import logging
import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

SOPHIE_URL = os.getenv("SOPHIE_URL", "http://127.0.0.1:8090")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

AUTHORIZED_IDS = set(int(x.strip()) for x in os.getenv("TELEGRAM_AUTHORIZED_IDS", "").split(",") if x.strip())

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AUTHORIZED_IDS and user_id not in AUTHORIZED_IDS:
        await update.message.reply_text("Unauthorized.")
        return
    await update.message.reply_text("Hi, I'm Sophie on Telegram! Send me a message.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if AUTHORIZED_IDS and user_id not in AUTHORIZED_IDS:
        await update.message.reply_text("Unauthorized.")
        return

    user_text = update.message.text
    log.info(f"User {user_id}: {user_text}")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{SOPHIE_URL}/chat",
                json={"message": user_text, "session_id": f"tg_{user_id}"},
            )
            if resp.status_code != 200:
                await update.message.reply_text(f"Error: {resp.text[:200]}")
                return

            full_reply = ""
            async for line in resp.aiter_lines():
                if not line:
                    continue
                try:
                    data = __import__('json').loads(line)
                    if data.get("token"):
                        full_reply += data["token"]
                    if data.get("done"):
                        break
                except:
                    pass

            if full_reply:
                if len(full_reply) > 4000:
                    for i in range(0, len(full_reply), 4000):
                        await update.message.reply_text(full_reply[i:i+4000])
                else:
                    await update.message.reply_text(full_reply)
            else:
                await update.message.reply_text("Sorry, I didn't understand that.")

    except Exception as e:
        log.error(f"Error: {e}")
        await update.message.reply_text(f"Sorry, something went wrong: {e}")

def main():
    if not BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set. Exiting.")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Telegram bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
