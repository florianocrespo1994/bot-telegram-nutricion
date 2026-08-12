import os
import logging
from flask import Flask
from threading import Thread

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

from nutrition_bot.config import get_settings
from nutrition_bot.gemini_service import GeminiNutritionService
from nutrition_bot.handlers import (
    error_handler,
    handle_media,
    handle_text,
    help_command,
    reminder_command,
    start_command,
    cancel_reminder_command,
    send_scheduled_reminder,
)

logger = logging.getLogger(__name__)

# 1. Servidor web obligatorio para que Render mantenga el servicio activo
app = Flask('')

@app.route('/')
def home():
    return "El bot está vivo"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

async def post_init(application: Application) -> None:
    bot_info = await application.bot.get_me()
    logger.info(
        "Bot de Telegram listo: @%s",
        bot_info.username or bot_info.first_name or "sin_username",
    )

def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "Falta TELEGRAM_BOT_TOKEN. Configura la variable de entorno en Render."
        )

    nutrition_service = GeminiNutritionService(settings)
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )
    application.bot_data["nutrition_service"] = nutrition_service
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("recordatorio", reminder_command))
    application.add_handler(
        CommandHandler("cancelar_recordatorio", cancel_reminder_command)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.VOICE | filters.AUDIO, handle_media)
    )
    application.add_error_handler(error_handler)

    if application.job_queue is None:
        raise RuntimeError(
            "JobQueue no está disponible."
        )
    application.job_queue.scheduler.timezone = settings.timezone
    application.bot_data["scheduled_reminder_callback"] = send_scheduled_reminder
    return application


if __name__ == "__main__":
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    
    # 1. Arrancamos Flask en segundo plano en el puerto que pide Render
    port = int(os.environ.get("PORT", 10000))
    flask_thread = Thread(target=lambda: app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False))
    flask_thread.daemon = True
    flask_thread.start()
    logger.info("Servidor web Flask iniciado en segundo plano.")

    # 2. Corrimos el bot de Telegram en el hilo principal (vital para que funcione el polling)
    logger.info("Iniciando bot de Telegram...")
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    # Arrancamos el bot de Telegram
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)
