import os
import json
import logging
import re
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

def extraer_calorias(texto):
    """Extrae el primer número seguido de 'kcal' o 'calorías' del texto de Gemini"""
    match = re.search(r'(\d+)\s*(?:kcal|calorías|cal)', texto, re.IGNORECASE)
    return int(match.group(1)) if match else 0

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
    text_input = update.message.text or update.message.caption
    if not text_input:
        await update.message.reply_text("Por favor, enviá texto o una descripción.")
        return

    service: GeminiNutritionService = context.application.bot_data["nutrition_service"]
    req = GeminiInput(text=text_input)

    try:
        ai_response = await service.analyze(req)
        
        # Guardamos el análisis en el contexto temporalmente
        context.user_data["pending_analysis"] = ai_response

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
        await update.message.reply_text("Ups, tuve un problema al procesar tu solicitud con la IA.")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm":
        analisis = context.user_data.pop("pending_analysis", "")
        today = datetime.now().strftime("%Y-%m-%d")
        user_id = str(query.from_user.id)
        
        kcal_detectadas = extraer_calorias(analisis)
        logs = get_db(LOGS_FILE)
        
        if user_id not in logs: logs[user_id] = {}
        if today not in logs[user_id]: 
            logs[user_id][today] = {"kcal_ing": 0, "kcal_quemadas": 0}
        
        # Clasificación automática según el contenido del análisis de la IA
        texto_lower = analisis.lower()
        if any(palabra in texto_lower for palabra in ["quemada", "entrenamiento", "actividad", "deporte", "minutos", "horas", "squash", "running"]):
            logs[user_id][today]["kcal_quemadas"] += kcal_detectadas
            tipo_msj = f"🔥 {kcal_detectadas} kcal quemadas registradas."
        else:
            logs[user_id][today]["kcal_ing"] += kcal_detectadas
            tipo_msj = f"🍽️ {kcal_detectadas} kcal ingeridas registradas."
            
        save_db(LOGS_FILE, logs)
        
        await query.edit_message_text(f"✅ ¡Guardado con éxito! ({tipo_msj}) 🚀", parse_mode="Markdown")
    
    elif query.data == "edit":
        await query.edit_message_text("✏️ Entendido. Escribime la descripción corregida:")

    elif query.data == "med_tip":
        tip_text = (
            "👨‍⚕️ *Perspectiva Médica y Nutricional:*\n\n"
            "Evaluar la densidad calórica y el timing de los macronutrientes es fundamental "
            "para optimizar la composición corporal sin descuidar la salud metabólica."
        )
        await query.message.reply_text(tip_text, parse_mode="Markdown")

    elif query.data == "download_report":
        await reporte_command(update, context)

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    profiles = get_db(PROFILES_FILE)
    logs = get_db(LOGS_FILE)
    
    user_profile = profiles.get(user_id, {})
    user_log = logs.get(user_id, {}).get(today, {"kcal_ing": 0, "kcal_quemadas": 0})
    
    kcal_objetivo = user_profile.get("kcal_objetivo", 2200)
    kcal_ing = user_log["kcal_ing"]
    kcal_quemadas = user_log["kcal_quemadas"]
    
    # Cálculo solicitado: Ingeridas - Quemadas (Balance diario neto)
    balance_diario = kcal_ing - kcal_quemadas
    
    await update.message.reply_text(
        f"📊 *Balance Diario ({today})*\n\n"
        f"• *Kcal diarias objetivo:* {kcal_objetivo} kcal\n"
        f"• *Kcal ingeridas:* {kcal_ing} kcal\n"
        f"• *Kcal quemadas:* {kcal_quemadas} kcal\n\n"
        f"⚖️ *Balance diario (Ingeridas - Quemadas):* {balance_diario:+d} kcal",
        parse_mode="Markdown"
    )

async def balance_general_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    profiles = get_db(PROFILES_FILE)
    logs_user = get_db(LOGS_FILE).get(user_id, {})
    
    profile = profiles.get(user_id, {})
    kcal_objetivo = profile.get("kcal_objetivo", 2200)
    
    total_ing = sum(d.get("kcal_ing", 0) for d in logs_user.values())
    total_quem = sum(d.get("kcal_quemadas", 0) for d in logs_user.values())
    balance_neto_total = total_ing - total_quem
    
    await update.message.reply_text(
        f"📈 *Balance General (Acumulado Mensual)*\n\n"
        f"• *Kcal Objetivo Promedio/Diario:* {kcal_objetivo} kcal\n"
        f"• *Total Kcal Ingeridas (Mes):* {total_ing} kcal\n"
        f"• *Total Kcal Quemadas (Mes):* {total_quem} kcal\n\n"
        f"⚖️ *Balance Neto Total (Ingeridas - Quemadas):* {balance_neto_total:+d} kcal",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📄 Descargar Reporte Completo", callback_data="download_report")]
        ]),
        parse_mode="Markdown"
    )

async def reporte_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filename = "reporte_nutricional.txt"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("REPORTE NUTRICIONAL Y METABÓLICO - DR. CRESPO\n")
        f.write("=============================================\n")
        f.write(f"Fecha de emisión: {datetime.now().strftime('%Y-%m-%d')}\n\n")
        f.write("Detalle acumulado mensual de ingesta, gasto energético y balance neto.\n")
    
    with open(filename, "rb") as f:
        await update.effective_message.reply_document(
            document=f, 
            filename="reporte_nutricional.txt", 
            caption="📄 Aquí tenés tu reporte mensual descargable."
        )

def build_application():
    settings = get_settings()
    application = Application.builder().token(settings.telegram_bot_token).build()
    
    application.bot_data["nutrition_service"] = GeminiNutritionService(settings)
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("balancegeneral", balance_general_command))
    application.add_handler(CommandHandler("reporte", reporte_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    return application

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    logger.info("Servidor web Flask iniciado.")

    app_bot = build_application()
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES)
