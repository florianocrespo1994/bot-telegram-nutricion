import os
import json
import logging
from datetime import datetime
from flask import Flask
from threading import Thread

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters,
)

from nutrition_bot.config import get_settings
from nutrition_bot.gemini_service import GeminiNutritionService, GeminiInput

# Configuración básica
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- PERSISTENCIA ---
PROFILES_FILE = "user_profiles.json"
LOGS_FILE = "user_logs.json"

def get_db(file):
    return json.load(open(file, "r", encoding="utf-8")) if os.path.exists(file) else {}

def save_db(file, data):
    with open(file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

# --- SERVIDOR FLASK (RENDER) ---
app = Flask('')
@app.route('/')
def home(): return "Bot activo y sincronizado."

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)

# --- COMANDOS BÁSICOS ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "¡Hola! Soy tu asistente de nutrición y actividad física. 🥑🎾\n"
        "Mandame un mensaje describiendo lo que comiste o la actividad física que realizaste, "
        "y lo procesamos al instante."
    )

# --- LÓGICA DEL BOT CONECTADA A TU GEMINI SERVICE ---

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Procesa el mensaje del usuario usando tu GeminiNutritionService real"""
    text_input = update.message.text or update.message.caption
    if not text_input:
        await update.message.reply_text("Por favor, enviá texto o una descripción de lo que consumiste o realizaste.")
        return

    # Usamos tu estructura GeminiInput exacta
    service: GeminiNutritionService = context.application.bot_data["nutrition_service"]
    req = GeminiInput(text=text_input)

    try:
        # Llamada asincrónica real a tu servicio de Gemini
        ai_response = await service.analyze(req)
        
        # Guardamos temporalmente el texto de la respuesta en el contexto por si confirma o pide info extra
        context.user_data["pending_analysis"] = ai_response

        # Respondemos de forma limpia con los botones de validación y el nuevo botón de Tip Médico
        await update.message.reply_text(
            f"📋 *Análisis del Asistente:*\n\n{ai_response}\n\n¿Estás de acuerdo con este registro?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Confirmar", callback_data="confirm"),
                    InlineKeyboardButton("✏️ Editar", callback_data="edit")
                ],
                [
                    InlineKeyboardButton("👨‍⚕️ Más Info / Tip Médico", callback_data="med_tip")
                ]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error procesando con Gemini: {e}")
        await update.message.reply_text("Ups, tuve un problema al procesar tu solicitud con la IA. Intentá de nuevo en un momento.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm":
        context.user_data.pop("pending_analysis", None)
        today = datetime.now().strftime("%Y-%m-%d")
        user_id = str(query.from_user.id)
        
        # Registro básico en JSON
        logs = get_db(LOGS_FILE)
        if user_id not in logs: logs[user_id] = {}
        if today not in logs[user_id]: logs[user_id][today] = {"registros": 0}
        
        logs[user_id][today]["registros"] += 1
        save_db(LOGS_FILE, logs)
        
        await query.edit_message_text("✅ ¡Guardado con éxito! Seguimos sumando. 🚀", parse_mode="Markdown")
    
    elif query.data == "edit":
        await query.edit_message_text("✏️ Entendido. Escribime la descripción corregida:")

    elif query.data == "med_tip":
        # Opción para ponerse la bata de médico y dar información complementaria
        last_analysis = context.user_data.get("pending_analysis", "este alimento")
        
        # Opcional: Podrías hacer otra consulta rápida a Gemini pidiéndole el tip, o estructurarlo directo:
        tip_text = (
            "👨‍⚕️ *Perspectiva Médica y Nutricional:*\n\n"
            "Analizando la densidad calórica y el perfil de macronutrientes de lo ingresado, "
            "es importante evaluar el timing de los alimentos en relación con tu gasto energético diario y tu fase metabólica actual. "
            "Priorizar alimentos de alta densidad nutricional ayuda a mantener la saciedad sin comprometer tu objetivo.\n\n"
            "*(Recordá que podés confirmar o editar tu registro más arriba)*"
        )
        await query.message.reply_text(tip_text, parse_mode="Markdown")

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logs = get_db(LOGS_FILE)
    today = datetime.now().strftime("%Y-%m-%d")
    user_logs = logs.get(str(update.effective_user.id), {}).get(today, {"registros": 0})
    
    await update.message.reply_text(
        f"📊 *Balance Diario ({today})*\nRegistros confirmados hoy: {user_logs['registros']}", 
        parse_mode="Markdown"
    )

def build_application():
    settings = get_settings()
    application = Application.builder().token(settings.telegram_bot_token).build()
    
    # Inyectamos tu servicio real
    application.bot_data["nutrition_service"] = GeminiNutritionService(settings)
    
    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    return application

if __name__ == "__main__":
    # Arrancar Flask en segundo plano para Render
    Thread(target=run_flask, daemon=True).start()
    logger.info("Servidor web Flask iniciado.")

    # Arrancar Telegram Bot
    app_bot = build_application()
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES)
