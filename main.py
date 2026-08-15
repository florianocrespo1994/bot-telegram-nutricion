import os
import json
import logging
import re
import csv
import io
from datetime import datetime
from flask import Flask
from threading import Thread

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler, 
    ContextTypes, filters, ConversationHandler
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
    match = re.search(r'(\d+)\s*(?:kcal|calorías|cal)', texto, re.IGNORECASE)
    return int(match.group(1)) if match else 0

# --- ESTADOS DEL ONBOARDING ---
SEXO, EDAD, PESO, ALTURA, ACTIVIDAD, OBJETIVO, DEPORTE = range(7)

# --- SERVIDOR FLASK (RENDER) ---
app = Flask('')
@app.route('/')
def home(): return "Bot activo y sincronizado."

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)


# --- ONBOARDING CLÍNICO (/start) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_keyboard = [['Hombre', 'Mujer']]
    await update.message.reply_text(
        "¡Bienvenido! Soy tu asistente de nutrición y actividad física. 🥑🎾\n\n"
        "Vamos a configurar tu perfil clínico para calcular tus requerimientos exactos.\n"
        "(Podés escribir /cancel en cualquier momento para salir).\n\n"
        "Para empezar, indicame tu *Sexo*:",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return SEXO

async def ask_edad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['sexo'] = update.message.text
    await update.message.reply_text("Perfecto. Ahora ingresá tu *Edad* (en números):", reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return EDAD

async def ask_peso(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['edad'] = int(update.message.text)
        await update.message.reply_text("Anotado. ¿Cuál es tu *Peso* en kg? (ej: 75.5):", parse_mode="Markdown")
        return PESO
    except ValueError:
        await update.message.reply_text("Por favor, ingresá un número válido para tu edad.")
        return EDAD

async def ask_altura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['peso'] = float(update.message.text.replace(',', '.'))
        await update.message.reply_text("Excelente. ¿Cuál es tu *Altura* en cm? (ej: 180):", parse_mode="Markdown")
        return ALTURA
    except ValueError:
        await update.message.reply_text("Por favor, ingresá un número válido para tu peso.")
        return PESO

async def ask_actividad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['altura'] = float(update.message.text)
        reply_keyboard = [['Sedentario', 'Leve'], ['Moderado', 'Intenso']]
        await update.message.reply_text(
            "Guardado. Seleccioná tu *Nivel de Actividad Física* diario:",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
            parse_mode="Markdown"
        )
        return ACTIVIDAD
    except ValueError:
        await update.message.reply_text("Por favor, ingresá un número válido para tu altura en cm.")
        return ALTURA

async def ask_objetivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['actividad'] = update.message.text
    reply_keyboard = [['Déficit Calorico', 'Mantenimiento', 'Volumen']]
    await update.message.reply_text(
        "Ya casi terminamos. ¿Cuál es tu *Objetivo Metabólico* actual?:",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return OBJETIVO

async def ask_deporte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['objetivo'] = update.message.text
    reply_keyboard = [
        ['Squash', 'Tenis', 'Pádel'], 
        ['Fútbol 11', 'Fútbol 5', 'Natación'],
        ['Baile', 'Saltar la cuerda', 'Boxeo'],
        ['Crossfit', 'Kick boxing', 'Judo'],
        ['Handball', 'Tenis de mesa', 'Ajedrez']
    ]
    await update.message.reply_text(
        "Por último, seleccioná el *Deporte Favorito* que realizas con frecuencia:",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return DEPORTE

async def finish_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    deporte = update.message.text
    sexo = context.user_data.get('sexo', 'Hombre')
    edad = context.user_data.get('edad', 30)
    peso = context.user_data.get('peso', 75.0)
    altura = context.user_data.get('altura', 180.0)
    actividad = context.user_data.get('actividad', 'Moderado')
    objetivo = context.user_data.get('objetivo', 'Mantenimiento')
    
    # --- CÁLCULO DE MIFFLIN-ST JEOR ---
    tmb = (10 * peso) + (6.25 * altura) - (5 * edad)
    tmb += 5 if sexo == 'Hombre' else -161
    
    multiplicadores = {'Sedentario': 1.2, 'Leve': 1.375, 'Moderado': 1.55, 'Intenso': 1.725}
    gasto_diario = tmb * multiplicadores.get(actividad, 1.2)
    
    ajustes = {'Déficit Calorico': -500, 'Mantenimiento': 0, 'Volumen': 500}
    kcal_objetivo = int(gasto_diario + ajustes.get(objetivo, 0))

    # Guardar en base de datos
    user_id = str(update.effective_user.id)
    profiles = get_db(PROFILES_FILE)
    profiles[user_id] = {
        "sexo": sexo, "edad": edad, "peso": peso, "altura": altura,
        "actividad": actividad, "objetivo": objetivo, "deporte": deporte,
        "kcal_objetivo": kcal_objetivo
    }
    save_db(PROFILES_FILE, profiles)

    resumen = (
        f"✅ *¡Perfil Clínico Configurado!*\n\n"
        f"• *Tasa Metabólica Basal:* ~{int(tmb)} kcal\n"
        f"• *Deporte:* {deporte}\n"
        f"🎯 *Tu objetivo calórico diario quedó seteado en: {kcal_objetivo} kcal*\n\n"
        f"Ya podés empezar a enviarme fotos, audios o textos de tus comidas y entrenamientos. 💪"
    )
    await update.message.reply_text(resumen, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return ConversationHandler.END

async def cancel_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Configuración cancelada. Podes usar /start cuando estés listo.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --- COMANDOS AUXILIARES ---

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🛠 *Comandos Disponibles:*\n\n"
        "• /start - Configurar perfil y recalcular objetivo\n"
        "• /balance - Muestra el balance diario neto\n"
        "• /balancegeneral - Muestra acumulados y descarga Excel\n"
        "• /setobjetivo <kcal> - Cambia tu meta de calorías manualmente\n"
        "• /eliminarultimo - Deshace el último registro guardado de hoy\n"
        "• /help - Muestra este menú"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def set_objetivo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        nuevo_obj = int(context.args[0])
        user_id = str(update.effective_user.id)
        profiles = get_db(PROFILES_FILE)
        if user_id not in profiles: profiles[user_id] = {}
        profiles[user_id]["kcal_objetivo"] = nuevo_obj
        save_db(PROFILES_FILE, profiles)
        await update.message.reply_text(f"🎯 Objetivo actualizado a *{nuevo_obj} kcal* diarias.", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Uso correcto: /setobjetivo 2500")

async def eliminar_ultimo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    today = datetime.now().strftime("%Y-%m-%d")
    logs = get_db(LOGS_FILE)
    last_action = context.user_data.get("last_action")
    
    if not last_action or last_action.get("date") != today:
        await update.message.reply_text("❌ No hay un registro reciente en esta sesión para deshacer.")
        return
        
    tipo = last_action["tipo"]
    kcal = last_action["kcal"]
    
    if user_id in logs and today in logs[user_id]:
        if tipo == "ingesta":
            logs[user_id][today]["kcal_ing"] = max(0, logs[user_id][today].get("kcal_ing", 0) - kcal)
        elif tipo == "gasto_cardio":
            logs[user_id][today]["kcal_quemadas_cardio"] = max(0, logs[user_id][today].get("kcal_quemadas_cardio", 0) - kcal)
            logs[user_id][today]["kcal_quemadas"] = max(0, logs[user_id][today].get("kcal_quemadas", 0) - kcal)
        elif tipo == "gasto_fuerza":
            logs[user_id][today]["kcal_quemadas_fuerza"] = max(0, logs[user_id][today].get("kcal_quemadas_fuerza", 0) - kcal)
            logs[user_id][today]["kcal_quemadas"] = max(0, logs[user_id][today].get("kcal_quemadas", 0) - kcal)
            
        save_db(LOGS_FILE, logs)
        context.user_data.pop("last_action", None)
        await update.message.reply_text(f"🗑️ Listo. Se revirtió el último registro de {kcal} kcal.")
    else:
        await update.message.reply_text("No encontré datos de hoy para modificar.")


# --- LÓGICA DE PROCESAMIENTO DE MENSAJES (GEMINI) ---

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text_input = message.text or message.caption or ""
    
    # Filtro anti-basura
    if len(text_input.strip()) < 3 and not message.photo and not message.voice and not message.audio:
        await message.reply_text("👍") 
        return

    # Control de estado "Editando"
    if context.user_data.get("esperando_edicion"):
        context.user_data["esperando_edicion"] = False
        text_input = f"Corrección sobre el registro anterior: {text_input}"

    service: GeminiNutritionService = context.application.bot_data["nutrition_service"]
    file_bytes = None
    mime_type = None
    media_label = None

    if message.photo:
        await message.reply_text("👀 Analizando la imagen... Un momento.")
        photo_file = await message.photo[-1].get_file()
        file_bytes = bytes(await photo_file.download_as_bytearray())
        mime_type = "image/jpeg"
        media_label = "fotografía de alimentos o bebidas"
        if not text_input: text_input = "Analiza los alimentos o bebidas de esta imagen y calcula las calorías y macronutrientes."

    elif message.voice or message.audio:
        await message.reply_text("🎧 Escuchando tu nota de voz...")
        voice_file = await message.voice.get_file() if message.voice else await message.audio.get_file()
        file_bytes = bytes(await voice_file.download_as_bytearray())
        mime_type = "audio/ogg"
        media_label = "nota de voz del usuario"
        text_input = text_input or "Procesa esta nota de voz sobre mi ingesta o actividad física."

    req = GeminiInput(text=text_input, media_bytes=file_bytes, mime_type=mime_type, media_label=media_label)

    try:
        ai_response = await service.analyze(req)
        
        # --- EXTRACCIÓN DE ETIQUETAS DEL PROMPT ---
        tipo_registro = "ingesta" 
        if "[TIPO: GASTO_CARDIO]" in ai_response: tipo_registro = "gasto_cardio"
        elif "[TIPO: GASTO_FUERZA]" in ai_response: tipo_registro = "gasto_fuerza"
            
        tip_medico = "Sigue prestando atención a tus porciones y actividad para mantener la salud metabólica."
        tip_match = re.search(r'\[TIP_MEDICO:\s*(.*?)\]', ai_response, re.DOTALL)
        if tip_match: tip_medico = tip_match.group(1).strip()
            
        clean_response = re.sub(r'\[TIPO:.*?\]', '', ai_response)
        clean_response = re.sub(r'\[TIP_MEDICO:.*?\]', '', clean_response).strip()
        
        context.user_data["pending_analysis"] = clean_response
        context.user_data["pending_tipo"] = tipo_registro
        context.user_data["pending_tip"] = tip_medico

        await message.reply_text(
            f"📋 *Análisis:*\n\n{clean_response}\n\n¿Registramos esto?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar", callback_data="confirm"), InlineKeyboardButton("✏️ Editar", callback_data="edit")],
                [InlineKeyboardButton("👨‍⚕️ Tip Médico", callback_data="med_tip")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error Gemini: {e}")
        await message.reply_text("¡Entendido! Lo procesé, pero por favor describime cantidades o tiempos para mayor exactitud. 💪")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "confirm":
        analisis = context.user_data.pop("pending_analysis", "")
        tipo = context.user_data.pop("pending_tipo", "ingesta")
        today = datetime.now().strftime("%Y-%m-%d")
        user_id = str(query.from_user.id)
        
        kcal_detectadas = extraer_calorias(analisis)
        logs = get_db(LOGS_FILE)
        
        if user_id not in logs: logs[user_id] = {}
        if today not in logs[user_id]: 
            logs[user_id][today] = {"kcal_ing": 0, "kcal_quemadas": 0, "kcal_quemadas_cardio": 0, "kcal_quemadas_fuerza": 0}
        
        # Clasificación guiada por la etiqueta de Gemini
        if tipo == "gasto_cardio":
            logs[user_id][today]["kcal_quemadas_cardio"] = logs[user_id][today].get("kcal_quemadas_cardio", 0) + kcal_detectadas
            logs[user_id][today]["kcal_quemadas"] = logs[user_id][today].get("kcal_quemadas", 0) + kcal_detectadas
            tipo_msj = f"🏃‍♂️ {kcal_detectadas} kcal de cardio registradas"
        elif tipo == "gasto_fuerza":
            logs[user_id][today]["kcal_quemadas_fuerza"] = logs[user_id][today].get("kcal_quemadas_fuerza", 0) + kcal_detectadas
            logs[user_id][today]["kcal_quemadas"] = logs[user_id][today].get("kcal_quemadas", 0) + kcal_detectadas
            tipo_msj = f"🏋️‍♂️ {kcal_detectadas} kcal de fuerza registradas"
        else:
            logs[user_id][today]["kcal_ing"] = logs[user_id][today].get("kcal_ing", 0) + kcal_detectadas
            tipo_msj = f"🍽️ {kcal_detectadas} kcal ingeridas registradas"
            
        save_db(LOGS_FILE, logs)
        context.user_data["last_action"] = {"date": today, "tipo": tipo, "kcal": kcal_detectadas}
        
        await query.edit_message_text(f"{analisis}\n\n✅ *{tipo_msj} en la base de datos.* 🚀", parse_mode="Markdown")
    
    elif query.data == "edit":
        context.user_data["esperando_edicion"] = True
        await query.edit_message_text("✏️ Modo edición. Escribime la descripción corregida:")

    elif query.data == "med_tip":
        tip_text = context.user_data.get("pending_tip", "Consulta siempre a tu profesional de cabecera.")
        await query.message.reply_text(f"👨‍⚕️ *Perspectiva Médica y Metabólica:*\n\n{tip_text}", parse_mode="Markdown")

    elif query.data == "download_report":
        await reporte_command(update, context)


# --- REPORTES Y BALANCE ---

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    today = datetime.now().strftime("%Y-%m-%d")
    
    profiles = get_db(PROFILES_FILE)
    logs = get_db(LOGS_FILE)
    
    user_profile = profiles.get(user_id, {})
    user_log = logs.get(user_id, {}).get(today, {"kcal_ing": 0, "kcal_quemadas": 0})
    
    kcal_objetivo = user_profile.get("kcal_objetivo", 2200)
    kcal_ing = user_log.get("kcal_ing", 0)
    kcal_quemadas = user_log.get("kcal_quemadas", 0)
    
    balance_diario = kcal_ing - kcal_quemadas
    kcal_restantes = kcal_objetivo - balance_diario
    
    await update.message.reply_text(
        f"📊 *Balance Diario ({today})*\n\n"
        f"• *Kcal diarias objetivo:* {kcal_objetivo} kcal\n"
        f"• *Kcal ingeridas:* {kcal_ing} kcal\n"
        f"• *Kcal quemadas:* {kcal_quemadas} kcal\n\n"
        f"⚖️ *Balance neto diario:* {balance_diario:+d} kcal\n"
        f"📉 *Calorías restantes para tu objetivo:* {kcal_restantes} kcal",
        parse_mode="Markdown"
    )

async def balance_general_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    profiles = get_db(PROFILES_FILE)
    logs_user = get_db(LOGS_FILE).get(user_id, {})
    
    kcal_objetivo = profiles.get(user_id, {}).get("kcal_objetivo", 2200)
    total_ing = sum(d.get("kcal_ing", 0) for d in logs_user.values())
    total_quem = sum(d.get("kcal_quemadas", 0) for d in logs_user.values())
    balance_neto = total_ing - total_quem
    
    await update.message.reply_text(
        f"📈 *Balance General (Acumulado Mensual)*\n\n"
        f"• *Objetivo Diario:* {kcal_objetivo} kcal\n"
        f"• *Total Ingerido:* {total_ing} kcal\n"
        f"• *Total Quemado:* {total_quem} kcal\n"
        f"⚖️ *Balance Neto Total:* {balance_neto:+d} kcal",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📄 Descargar Reporte en Excel", callback_data="download_report")]]),
        parse_mode="Markdown"
    )

async def reporte_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    logs_user = get_db(LOGS_FILE).get(user_id, {})
    
    if not logs_user:
        await update.effective_message.reply_text("Todavía no hay registros para exportar.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    # Agregamos las columnas específicas de Cardio y Fuerza al Excel
    writer.writerow(["Fecha", "Kcal Ingeridas", "Quemadas (Cardio)", "Quemadas (Fuerza)", "Total Quemadas", "Balance Neto"])
    
    for fecha, datos in sorted(logs_user.items()):
        ing = datos.get("kcal_ing", 0)
        cardio = datos.get("kcal_quemadas_cardio", 0)
        fuerza = datos.get("kcal_quemadas_fuerza", 0)
        total_quemadas = datos.get("kcal_quemadas", 0)
        balance = ing - total_quemadas
        writer.writerow([fecha, ing, cardio, fuerza, total_quemadas, balance])
    
    csv_bytes = io.BytesIO(output.getvalue().encode('utf-8'))
    csv_bytes.name = f"reporte_metabolico_{datetime.now().strftime('%Y%m%d')}.csv"
    
    await update.effective_message.reply_document(
        document=csv_bytes, 
        caption="📊 Aquí tenés tu reporte. Abrilo con Excel para ver el balance entre Cardio y Fuerza."
    )


def build_application():
    settings = get_settings()
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["nutrition_service"] = GeminiNutritionService(settings)
    
    # Manejador de la conversación de Onboarding (/start)
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            SEXO: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_edad)],
            EDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_peso)],
            PESO: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_altura)],
            ALTURA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_actividad)],
            ACTIVIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_objetivo)],
            OBJETIVO: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_deporte)],
            DEPORTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, finish_onboarding)],
        },
        fallbacks=[CommandHandler('cancel', cancel_onboarding)]
    )
    
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("setobjetivo", set_objetivo_command))
    application.add_handler(CommandHandler("eliminarultimo", eliminar_ultimo_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("balancegeneral", balance_general_command))
    
    # Manejador general para procesar audios, fotos y textos
    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO) & ~filters.COMMAND, handle_input))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    return application

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    logger.info("Servidor web Flask iniciado.")
    app_bot = build_application()
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES)
