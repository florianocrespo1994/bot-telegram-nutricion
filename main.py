import os
import json
import logging
import re
import csv
import io
import random
import threading
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from flask import Flask
from threading import Thread

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

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

# --- PERSISTENCIA Y CANDADOS (Race Conditions) ---
PROFILES_FILE = "user_profiles.json"
LOGS_FILE = "user_logs.json"
db_lock = threading.Lock()

ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
QUECOMO_COOLDOWN_SECONDS = 30

RANGO_EDAD = (10, 100)
RANGO_PESO = (30.0, 300.0)
RANGO_ALTURA = (120.0, 230.0)

PISO_KCAL = {"Hombre": 1500, "Mujer": 1200}
FACTOR_PROTEINA = {"Déficit Calorico": 2.0, "Mantenimiento": 1.6, "Volumen": 1.8}
MULTIPLICADORES = {'Sedentario': 1.2, 'Leve': 1.375, 'Moderado': 1.55, 'Intenso': 1.725}

MET_TABLE = {
    'Squash': 7.3, 'Tenis': 7.3, 'Pádel': 6.0, 'Running': 9.8,
    'Cinta Correr': 8.3, 'Cinta Inclinada': 9.0, 'Fútbol 11': 7.0, 'Fútbol 5': 7.0,
    'Natación': 7.0, 'Baile': 4.8, 'Saltar la cuerda': 10.0, 'Boxeo': 7.8,
    'Crossfit': 8.0, 'Kick boxing': 8.3, 'Judo': 10.3, 'Handball': 8.0,
    'Tenis de mesa': 4.0, 'Ajedrez': 1.5,
}
MET_CAMINATA = 4.3
COMODINES_POR_SEMANA = 2

FRASES_MOTIVACION_EXTRA = [
    "🔥 Dato random: la consistencia registrando pesa más que la perfección en cada comida.",
    "💧 No te olvides de hidratarte, acompaña un montón al metabolismo.",
    "🧠 Cada registro que hacés entrena también el hábito, no solo el cuerpo.",
    "😴 El descanso es tan parte del plan como la comida y el entrenamiento.",
    "📈 Los promedios de varios días dicen más que un solo día aislado.",
]


def get_db(file):
    with db_lock:
        return json.load(open(file, "r", encoding="utf-8")) if os.path.exists(file) else {}


def save_db(file, data):
    with db_lock:
        tmp_file = f"{file}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        os.replace(tmp_file, file)


def extraer_calorias(texto):
    match = re.search(r'(\d+)\s*(?:kcal|calorías|cal)', texto, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def estimar_calorias_entrenamiento(texto, peso_kg, deporte_preferido):
    match_hs = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:hs|hora|horas)', texto, re.IGNORECASE)
    match_min = re.search(r'(\d+)\s*(?:min|minutos)', texto, re.IGNORECASE)
    
    minutos = 0
    if match_hs:
        minutos += float(match_hs.group(1).replace(',', '.')) * 60
    if match_min:
        minutos += int(match_min.group(1))
    
    if minutos == 0:
        minutos = 60

    met = MET_TABLE.get(deporte_preferido, 7.0)
    for dep, m in MET_TABLE.items():
        if dep.lower() in texto.lower():
            met = m
            break

    kcal_estimadas = int(met * 3.5 * peso_kg / 200 * minutos)
    return max(50, kcal_estimadas)


def extraer_macros(texto):
    patron = r'(\d+(?:[.,]\d+)?)\s*g\s*P.*?(\d+(?:[.,]\d+)?)\s*g\s*C.*?(\d+(?:[.,]\d+)?)\s*g\s*G'
    match = re.search(patron, texto, re.IGNORECASE | re.DOTALL)
    if match:
        return {
            "proteinas": float(match.group(1).replace(',', '.')),
            "carbohidratos": float(match.group(2).replace(',', '.')),
            "grasas": float(match.group(3).replace(',', '.')),
        }
    return {"proteinas": 0.0, "carbohidratos": 0.0, "grasas": 0.0}


JSON_BLOCK_PATTERN = re.compile(r'###DATOS_JSON###\s*(\{.*?\})\s*###FIN_DATOS###', re.DOTALL)


def _normalizar_tipo(valor):
    v = str(valor).strip().upper()
    if v == "GASTO_CARDIO": return "gasto_cardio"
    if v == "GASTO_FUERZA": return "gasto_fuerza"
    return "ingesta"

# --- FIREWALL DE INGESTA (La Regla de Oro) ---
def extraer_datos_estructurados(texto, peso_usuario=75.0, deporte_preferido="Squash"):
    match = JSON_BLOCK_PATTERN.search(texto)
    tipo = "ingesta"  # Por defecto siempre es comida
    kcal = 0
    proteinas, carbohidratos, grasas = 0.0, 0.0, 0.0
    tip_medico = "Sigue prestando atención a tus porciones y actividad."

    if match:
        try:
            data = json.loads(match.group(1))
            tipo_crudo = str(data.get("tipo", "INGESTA")).strip().upper()
            
            if "GASTO" in tipo_crudo:
                tipo = _normalizar_tipo(tipo_crudo)
            else:
                tipo = "ingesta"

            kcal = int(data.get("kcal", 0) or 0)
            proteinas = float(data.get("proteinas_g", 0) or 0)
            carbohidratos = float(data.get("carbohidratos_g", 0) or 0)
            grasas = float(data.get("grasas_g", 0) or 0)
            tip_medico = str(data.get("tip_medico") or tip_medico)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning(f"Error parseando JSON: {e}")

    if not match:
        kcal = extraer_calorias(texto)
        macros = extraer_macros(texto)
        proteinas, carbohidratos, grasas = macros["proteinas"], macros["carbohidratos"], macros["grasas"]
        tip_match = re.search(r'\[TIP_MEDICO:\s*(.*?)\]', texto, re.DOTALL)
        if tip_match: tip_medico = tip_match.group(1).strip()
        tipo = "ingesta"

    # Filtro final de seguridad: Pisamos cualquier error de la IA si detectamos comida
    texto_lower = texto.lower()
    palabras_comida = ["almuerzo", "cena", "desayuno", "merienda", "comí", "comia", "plato", "gramos", "grs", "gramo", "huevos", "huevo", "ensalada", "fideos", "carne", "pollo", "pan", "queso", "papa", "milanesa", "tarta", "pizza"]
    es_entrenamiento_real = any(w in texto_lower for w in ["entren", "fui al gym", "fui al gimnasio", "jugué", "jugue al", "partido de", "corrí", "correr", "cinta", "natación", "natacion", "crossfit", "pedalear", "pedaleé"])

    if any(w in texto_lower for w in palabras_comida):
        tipo = "ingesta"
    elif es_entrenamiento_real and not any(w in texto_lower for w in palabras_comida):
        if "fuerza" in texto_lower or "gym" in texto_lower or "gimnasio" in texto_lower:
            tipo = "gasto_fuerza"
        else:
            tipo = "gasto_cardio"

    if tipo in ("gasto_cardio", "gasto_fuerza") and kcal == 0:
        kcal = estimar_calorias_entrenamiento(texto, peso_usuario, deporte_preferido)

    return {
        "tipo": tipo, "kcal": kcal,
        "proteinas": proteinas, "carbohidratos": carbohidratos, "grasas": grasas,
        "tip_medico": tip_medico,
    }


def limpiar_respuesta(texto):
    limpio = JSON_BLOCK_PATTERN.sub('', texto)
    limpio = re.sub(r'\[TIPO:.*?\]', '', limpio)
    limpio = re.sub(r'\[TIP_MEDICO:.*?\]', '', limpio)
    return limpio.strip()


def get_fecha_argentina():
    return datetime.now(ARG_TZ).strftime("%Y-%m-%d")


def get_fecha_ayer_argentina():
    return (datetime.now(ARG_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


def _dia_vacio():
    return {
        "kcal_ing": 0, "kcal_quemadas": 0, "kcal_quemadas_cardio": 0, "kcal_quemadas_fuerza": 0,
        "proteinas": 0.0, "carbohidratos": 0.0, "grasas": 0.0,
    }

# --- CÁLCULO CLÍNICO METABÓLICO (Con %) ---
def calcular_perfil_calorico(sexo, edad, peso, altura, actividad, objetivo):
    tmb = (10 * peso) + (6.25 * altura) - (5 * edad)
    tmb += 5 if sexo == 'Hombre' else -161
    gasto_diario = tmb * MULTIPLICADORES.get(actividad, 1.2)
    
    # Cálculo porcentual clínico de calorías
    if objetivo == 'Déficit Calorico':
        kcal_calculado = gasto_diario * 0.80  # -20%
    elif objetivo == 'Volumen':
        kcal_calculado = gasto_diario * 1.10  # +10%
    else:
        kcal_calculado = gasto_diario

    piso = PISO_KCAL.get(sexo, 1200)
    aviso_piso = kcal_calculado < piso
    kcal_objetivo = int(piso if aviso_piso else kcal_calculado)

    factor_proteina = FACTOR_PROTEINA.get(objetivo, 1.6)
    objetivo_proteina_g = round(peso * factor_proteina, 1)

    return {
        "tmb": tmb, "kcal_objetivo": kcal_objetivo,
        "aviso_piso": aviso_piso, "objetivo_proteina_g": objetivo_proteina_g,
    }


def actualizar_historial_peso(perfil, fecha, peso):
    historial = perfil.setdefault("historial_peso", [])
    historial = [h for h in historial if h["fecha"] != fecha]
    historial.append({"fecha": fecha, "peso": peso})
    historial.sort(key=lambda h: h["fecha"])
    perfil["historial_peso"] = historial[-30:]
    return perfil["historial_peso"]


def promedio_peso_7d(historial, fecha_actual_str):
    fecha_actual = datetime.strptime(fecha_actual_str, "%Y-%m-%d").date()
    recientes = [
        h["peso"] for h in historial
        if (fecha_actual - datetime.strptime(h["fecha"], "%Y-%m-%d").date()).days <= 6
    ]
    return sum(recientes) / len(recientes) if recientes else None


def calcular_opciones_ejercicio(kcal_exceso, peso_kg, deporte_preferido):
    opciones = []
    met_pref = MET_TABLE.get(deporte_preferido)
    if met_pref and met_pref >= 3:
        kcal_min_pref = met_pref * 3.5 * peso_kg / 200
        opciones.append((deporte_preferido, max(1, round(kcal_exceso / kcal_min_pref))))
    kcal_min_caminata = MET_CAMINATA * 3.5 * peso_kg / 200
    opciones.append(("caminata rápida", max(1, round(kcal_exceso / kcal_min_caminata))))
    return opciones


def _lunes_de_semana(fecha_str):
    fecha = datetime.strptime(fecha_str, "%Y-%m-%d").date()
    return (fecha - timedelta(days=fecha.weekday())).strftime("%Y-%m-%d")


def actualizar_comidas_frecuentes(perfil, texto_resumen, kcal, macros):
    lista = perfil.setdefault("comidas_frecuentes", [])
    clave = texto_resumen.strip().lower()[:60]
    lista = [c for c in lista if c.get("clave") != clave]
    lista.insert(0, {"clave": clave, "texto": texto_resumen[:80], "kcal": kcal, "macros": macros})
    perfil["comidas_frecuentes"] = lista[:5]


def verificar_y_generar_refuerzo_superavit(perfil, dia):
    if dia.get("aviso_superavit_enviado"):
        return None
    kcal_objetivo = dia.get("objetivo_temporal", perfil.get("kcal_objetivo", 2200))
    balance = dia.get("kcal_ing", 0) - dia.get("kcal_quemadas", 0)
    exceso = balance - kcal_objetivo
    if exceso <= 0:
        return None
    dia["aviso_superavit_enviado"] = True
    peso = perfil.get("peso", 75.0)
    deporte = perfil.get("deporte", "")
    opciones = calcular_opciones_ejercicio(exceso, peso, deporte)
    lineas = "\n".join(f"• ~{minutos} min de {nombre}" for nombre, minutos in opciones)
    return (
        f"💪 *¡Vas con energía hoy!* Estás {exceso} kcal por encima de tu objetivo diario.\n\n"
        f"Si en algún momento te copa moverte, con cualquiera de estas opciones lo emparejás:\n{lineas}\n\n"
        f"Ojo: esto es solo información, no una obligación — un día de superávit es parte normal "
        f"de cualquier proceso. Vos decidís. 🎾"
    )


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
        "¡Bienvenido! Soy tu asistente médico y deportivo. 🥑🎾\n\n"
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
        edad = int(update.message.text)
    except ValueError:
        await update.message.reply_text("Por favor, ingresá un número válido para tu edad.")
        return EDAD
    if not (RANGO_EDAD[0] <= edad <= RANGO_EDAD[1]):
        await update.message.reply_text(f"⚠️ Ingresá una edad entre {RANGO_EDAD[0]} y {RANGO_EDAD[1]} años.")
        return EDAD
    context.user_data['edad'] = edad
    await update.message.reply_text("Anotado. ¿Cuál es tu *Peso* en kg? (ej: 75.5):", parse_mode="Markdown")
    return PESO

async def ask_altura(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        peso = float(update.message.text.replace(',', '.'))
    except ValueError:
        await update.message.reply_text("Por favor, ingresá un número válido para tu peso.")
        return PESO
    if not (RANGO_PESO[0] <= peso <= RANGO_PESO[1]):
        await update.message.reply_text(f"⚠️ Ingresá un peso entre {RANGO_PESO[0]:.0f} y {RANGO_PESO[1]:.0f} kg.")
        return PESO
    context.user_data['peso'] = peso
    await update.message.reply_text("Excelente. ¿Cuál es tu *Altura* en cm? (ej: 180):", parse_mode="Markdown")
    return ALTURA

async def ask_actividad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        altura = float(update.message.text)
    except ValueError:
        await update.message.reply_text("Por favor, ingresá un número válido para tu altura en cm.")
        return ALTURA
    if not (RANGO_ALTURA[0] <= altura <= RANGO_ALTURA[1]):
        await update.message.reply_text(f"⚠️ Ingresá una altura entre {RANGO_ALTURA[0]:.0f} y {RANGO_ALTURA[1]:.0f} cm.")
        return ALTURA
    context.user_data['altura'] = altura
    reply_keyboard = [['Sedentario', 'Leve'], ['Moderado', 'Intenso']]
    await update.message.reply_text(
        "Guardado. Seleccioná tu *Nivel de Actividad Física* diario:",
        reply_markup=ReplyKeyboardMarkup(reply_keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return ACTIVIDAD

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
        ['Running', 'Cinta Correr', 'Cinta Inclinada'],
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

    resultado = calcular_perfil_calorico(sexo, edad, peso, altura, actividad, objetivo)
    today = get_fecha_argentina()

    user_id = str(update.effective_user.id)
    profiles = get_db(PROFILES_FILE)
    perfil = {
        "sexo": sexo, "edad": edad, "peso": peso, "altura": altura,
        "actividad": actividad, "objetivo": objetivo, "deporte": deporte,
        "kcal_objetivo": resultado["kcal_objetivo"],
        "objetivo_proteina_g": resultado["objetivo_proteina_g"],
    }
    actualizar_historial_peso(perfil, today, peso)
    profiles[user_id] = perfil
    save_db(PROFILES_FILE, profiles)

    logs = get_db(LOGS_FILE)
    if user_id in logs and today in logs[user_id]:
        logs[user_id][today].pop("objetivo_temporal", None)
        save_db(LOGS_FILE, logs)

    aviso_piso = (
        f"\n⚠️ Tu cálculo teórico daba menos que el mínimo seguro, así que seteé un piso de "
        f"{resultado['kcal_objetivo']} kcal. Te recomiendo consultar con un profesional presencial "
        f"para un plan más ajustado a tu caso.\n" if resultado["aviso_piso"] else ""
    )

    resumen = (
        f"✅ *¡Perfil Clínico Configurado!*\n\n"
        f"• *Tasa Metabólica Basal:* ~{int(resultado['tmb'])} kcal\n"
        f"• *Deporte:* {deporte}\n"
        f"🎯 *Tu objetivo calórico diario quedó seteado en: {resultado['kcal_objetivo']} kcal*\n"
        f"🥩 *Objetivo de proteína:* ~{resultado['objetivo_proteina_g']}g/día\n"
        f"{aviso_piso}\n"
        f"Para ver todo lo que puedo hacer, escribí /guia 💪"
    )
    await update.message.reply_text(resumen, reply_markup=ReplyKeyboardRemove(), parse_mode="Markdown")
    return ConversationHandler.END

async def cancel_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Configuración cancelada. Podes usar /start cuando estés listo.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# --- COMANDOS AVANZADOS Y GUÍA ---

async def guia_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    guia_text = (
        "📖 *GUÍA COMPLETA DEL BOT*\n\n"
        "🗣 *¿Cómo registrar?*\n"
        "Mandame un texto, foto o nota de voz diciendo qué comiste o cuánto entrenaste. Yo me encargo del resto.\n\n"
        "⚡ *Comandos Estrella:*\n"
        "• /quecomo - Analiza las kcal que te faltan y te arma el plan fraccionado.\n"
        "• /partidomanana - Sube tu objetivo de hoy (+350 kcal).\n"
        "• /peso <kg> - Recalculo tu metabolismo (con promedio de los últimos 7 días).\n"
        "• /ayer <descripción> - ¿Te olvidaste de registrar algo de ayer? Cargalo acá.\n"
        "• /rapido - Registrá en un toque una de tus comidas frecuentes.\n\n"
        "🔥 *Constancia:*\n"
        "• /racha - Mostrá tu racha actual y comodines disponibles.\n"
        "• /horarios HH:MM HH:MM - Personalizá tus horarios de recordatorio.\n\n"
        "📊 *Balance y Edición:*\n"
        "• /balance - Balance diario, incluye macros.\n"
        "• /balancegeneral - Promedios diarios y exporta Excel.\n"
        "• /eliminarultimo - Deshace el último guardado de hoy.\n"
        "• /setobjetivo <kcal> - Cambia tu meta manually.\n"
        "• /reporte - Descarga tu archivo Excel."
    )
    await update.message.reply_text(guia_text, parse_mode="Markdown")

async def peso_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        nuevo_peso = float(context.args[0].replace(',', '.'))
        if not (RANGO_PESO[0] <= nuevo_peso <= RANGO_PESO[1]):
            await update.message.reply_text(f"⚠️ Ingresá un peso entre {RANGO_PESO[0]:.0f} y {RANGO_PESO[1]:.0f} kg.")
            return

        user_id = str(update.effective_user.id)
        profiles = get_db(PROFILES_FILE)
        if user_id not in profiles:
            await update.message.reply_text("Primero configurá tu perfil con /start")
            return

        p = profiles[user_id]
        today = get_fecha_argentina()
        historial = actualizar_historial_peso(p, today, nuevo_peso)
        peso_prom = promedio_peso_7d(historial, today) or nuevo_peso

        resultado = calcular_perfil_calorico(p['sexo'], p['edad'], peso_prom, p['altura'], p['actividad'], p['objetivo'])
        p['peso'] = nuevo_peso
        p['kcal_objetivo'] = resultado['kcal_objetivo']
        p['objetivo_proteina_g'] = resultado['objetivo_proteina_g']
        save_db(PROFILES_FILE, profiles)

        logs = get_db(LOGS_FILE)
        if user_id in logs and today in logs[user_id]:
            logs[user_id][today].pop("objetivo_temporal", None)
            save_db(LOGS_FILE, logs)

        aviso_piso = (
            f"\n⚠️ Tu cálculo daba menos que el mínimo seguro, seteé el piso de {resultado['kcal_objetivo']} kcal."
            if resultado["aviso_piso"] else ""
        )
        await update.message.reply_text(
            f"⚖️ Peso actualizado a {nuevo_peso} kg (promedio de 7 días: {peso_prom:.1f} kg).\n"
            f"🎯 Tu nuevo objetivo diario: *{resultado['kcal_objetivo']} kcal*, "
            f"proteína: *{resultado['objetivo_proteina_g']}g*.{aviso_piso}",
            parse_mode="Markdown"
        )
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Uso correcto: /peso 74.5")

async def partidomanana_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    today = get_fecha_argentina()

    profiles = get_db(PROFILES_FILE)
    if user_id not in profiles:
        await update.message.reply_text("Por favor, configurá tu perfil con /start primero.")
        return

    objetivo_base = profiles[user_id].get("kcal_objetivo", 2200)
    objetivo_partido = objetivo_base + 350

    logs = get_db(LOGS_FILE)
    if user_id not in logs: logs[user_id] = {}
    if today not in logs[user_id]: logs[user_id][today] = _dia_vacio()

    logs[user_id][today]["objetivo_temporal"] = objetivo_partido
    save_db(LOGS_FILE, logs)

    await update.message.reply_text(
        f"🎾 *¡Protocolo Match Day Activado!*\n\n"
        f"Preparando los depósitos de glucógeno para el partido de mañana.\n"
        f"🎯 *Objetivo de hoy ajustado temporalmente a:* {objetivo_partido} kcal (+350 kcal).\n"
        f"Te sugiero priorizar carbohidratos complejos en tu cena. ¡A romperla!",
        parse_mode="Markdown"
    )

async def quecomo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_ts = datetime.now(ARG_TZ).timestamp()
    last_call = context.user_data.get("last_quecomo_call", 0)
    elapsed = now_ts - last_call
    if elapsed < QUECOMO_COOLDOWN_SECONDS:
        restante = int(QUECOMO_COOLDOWN_SECONDS - elapsed)
        await update.message.reply_text(f"⏳ Esperá {restante}s antes de pedir otra sugerencia.")
        return
    context.user_data["last_quecomo_call"] = now_ts

    user_id = str(update.effective_user.id)
    today = get_fecha_argentina()

    profiles = get_db(PROFILES_FILE)
    logs = get_db(LOGS_FILE)

    user_profile = profiles.get(user_id, {})
    user_log = logs.get(user_id, {}).get(today, {})

    kcal_objetivo = user_log.get("objetivo_temporal", user_profile.get("kcal_objetivo", 2200))
    kcal_ing = user_log.get("kcal_ing", 0)
    kcal_quemadas = user_log.get("kcal_quemadas", 0)

    balance_diario = kcal_ing - kcal_quemadas
    kcal_restantes = kcal_objetivo - balance_diario

    if kcal_restantes <= 50:
        await update.message.reply_text("¡Ya alcanzaste tu objetivo de hoy! Preparate un buen mate y a descansar el cuerpo. 🧉")
        return

    await update.message.reply_text(f"🔍 Analizando cómo distribuir tus {kcal_restantes} kcal restantes...", parse_mode="Markdown")

    service: GeminiNutritionService = context.application.bot_data["nutrition_service"]

    if kcal_restantes > 1000:
        instruccion = f"Al usuario le faltan {kcal_restantes} kcal. Como es un volumen alto, dividilo obligatoriamente en 2 tiempos de comida: una Merienda (aprox 400-500 kcal) y una Cena Completa (el resto). Detallá ambas opciones con cantidades y macros."
    else:
        instruccion = f"Al usuario le faltan {kcal_restantes} kcal. Sugerí una opción clara y rápida para cubrir este remanente con sus calorías y macros."

    prompt_bot = f"{instruccion} No uses etiquetas ocultas. Háblale directo como colega médico, sé breve y bien estructurado."
    req = GeminiInput(text=prompt_bot, media_bytes=None, mime_type=None, media_label=None)

    try:
        ai_response = await service.analyze_with_retry(req)
        clean_response = limpiar_respuesta(ai_response)
        await update.message.reply_text(f"🍽️ *Estrategia sugerida para cerrar el día:*\n\n{clean_response}", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error Gemini quecomo: {e}")
        await update.message.reply_text("Me costó procesar el cálculo. Te sugiero dividirlo en una porción de proteína magra con vegetales y una fuente de hidratos complejos.")

async def set_objetivo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        nuevo_obj = int(context.args[0])
        user_id = str(update.effective_user.id)
        profiles = get_db(PROFILES_FILE)
        if user_id not in profiles: profiles[user_id] = {}
        sexo = profiles[user_id].get("sexo", "Hombre")
        piso = PISO_KCAL.get(sexo, 1200)
        aviso = f"\n⚠️ Ojo, esto está por debajo del mínimo clínico recomendado ({piso} kcal). Te lo dejo igual porque lo pediste manualmente, pero te sugiero consultar con un profesional." if nuevo_obj < piso else ""
        profiles[user_id]["kcal_objetivo"] = nuevo_obj
        save_db(PROFILES_FILE, profiles)

        today = get_fecha_argentina()
        logs = get_db(LOGS_FILE)
        if user_id in logs and today in logs[user_id]:
            logs[user_id][today].pop("objetivo_temporal", None)
            save_db(LOGS_FILE, logs)

        await update.message.reply_text(f"🎯 Objetivo actualizado a *{nuevo_obj} kcal* diarias.{aviso}", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Uso correcto: /setobjetivo 2500")

async def eliminar_ultimo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    today = get_fecha_argentina()
    logs = get_db(LOGS_FILE)

    dia = logs.get(user_id, {}).get(today)
    last_action = dia.get("last_action") if dia else None

    if not last_action:
        await update.message.reply_text("❌ No hay un registro reciente hoy para deshacer.")
        return

    tipo = last_action["tipo"]
    kcal = last_action["kcal"]
    macros = last_action.get("macros", {"proteinas": 0.0, "carbohidratos": 0.0, "grasas": 0.0})

    if tipo == "ingesta":
        dia["kcal_ing"] = max(0, dia.get("kcal_ing", 0) - kcal)
        dia["proteinas"] = max(0.0, dia.get("proteinas", 0.0) - macros.get("proteinas", 0.0))
        dia["carbohidratos"] = max(0.0, dia.get("carbohidratos", 0.0) - macros.get("carbohidratos", 0.0))
        dia["grasas"] = max(0.0, dia.get("grasas", 0.0) - macros.get("grasas", 0.0))
    elif tipo == "gasto_cardio":
        dia["kcal_quemadas_cardio"] = max(0, dia.get("kcal_quemadas_cardio", 0) - kcal)
        dia["kcal_quemadas"] = max(0, dia.get("kcal_quemadas", 0) - kcal)
    elif tipo == "gasto_fuerza":
        dia["kcal_quemadas_fuerza"] = max(0, dia.get("kcal_quemadas_fuerza", 0) - kcal)
        dia["kcal_quemadas"] = max(0, dia.get("kcal_quemadas", 0) - kcal)

    dia.pop("last_action", None)
    dia["aviso_superavit_enviado"] = False
    save_db(LOGS_FILE, logs)
    await update.message.reply_text(f"🗑️ Listo. Se revirtió el último registro de {kcal} kcal.")


# --- REGISTRO RÁPIDO (/rapido) ---

async def rapido_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    profiles = get_db(PROFILES_FILE)
    frecuentes = profiles.get(user_id, {}).get("comidas_frecuentes", [])
    if not frecuentes:
        await update.message.reply_text("Todavía no tengo comidas frecuentes tuyas guardadas. A medida que registres, te las voy a sugerir acá. 🍽️")
        return
    context.user_data["quick_options"] = frecuentes
    botones = [[InlineKeyboardButton(f"{c['texto']} ({c['kcal']} kcal)", callback_data=f"quick:{i}")] for i, c in enumerate(frecuentes)]
    await update.message.reply_text("⚡ *Registro rápido:* elegí una de tus comidas frecuentes.", reply_markup=InlineKeyboardMarkup(botones), parse_mode="Markdown")


# --- RACHA Y COMODINES ---

async def racha_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    profiles = get_db(PROFILES_FILE)
    perfil = profiles.get(user_id, {})
    racha = perfil.get("racha_actual", 0)
    comodines = perfil.get("comodines", COMODINES_POR_SEMANA)
    await update.message.reply_text(
        f"🔥 *Racha actual:* {racha} día(s)\n🧊 *Comodines disponibles esta semana:* {comodines}\n\n"
        f"Los comodines protegen tu racha si te salteás un día — se renuevan cada lunes.",
        parse_mode="Markdown"
    )


# --- HORARIOS PERSONALIZADOS ---

async def _chequeo_individual(context: ContextTypes.DEFAULT_TYPE, es_mediodia: bool):
    user_id = str(context.job.chat_id)
    today = get_fecha_argentina()
    logs = get_db(LOGS_FILE)
    dia = logs.setdefault(user_id, {}).setdefault(today, _dia_vacio())
    kcal_ing = dia.get("kcal_ing", 0)
    kcal_quemadas = dia.get("kcal_quemadas", 0)

    if es_mediodia:
        dia["checkpoint_14hs"] = {"kcal_ing": kcal_ing, "kcal_quemadas": kcal_quemadas}
        disparar = kcal_ing == 0 and kcal_quemadas == 0
        texto = ("⏱️ *Recordatorio de Media Jornada*\n\n¡Hola! Aún no has registrado actividad y/o alimento el día de hoy. "
                 "No olvides hacerlo para tener un correcto análisis mensual. 🥑")
    else:
        checkpoint = dia.get("checkpoint_14hs")
        if checkpoint is not None:
            disparar = (kcal_ing == checkpoint.get("kcal_ing", 0) and kcal_quemadas == checkpoint.get("kcal_quemadas", 0))
        else:
            disparar = (kcal_ing == 0 and kcal_quemadas == 0)
        texto = ("🌙 *Cierre de Jornada*\n\nColega, aún no has registrado actividad y/o alimento el día de hoy. "
                 "No olvides hacerlo para tener un correcto análisis mensual. ¡A descansar!")

    save_db(LOGS_FILE, logs)
    if disparar:
        try:
            await context.bot.send_message(chat_id=user_id, text=texto, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"No se pudo enviar recordatorio individual a {user_id}: {e}")

async def chequeo_registro_individual_mediodia(context: ContextTypes.DEFAULT_TYPE):
    await _chequeo_individual(context, es_mediodia=True)

async def chequeo_registro_individual_noche(context: ContextTypes.DEFAULT_TYPE):
    await _chequeo_individual(context, es_mediodia=False)


def _programar_jobs_horario(job_queue, chat_id, hora1_str, hora2_str):
    for name in (f"mediodia:{chat_id}", f"noche:{chat_id}"):
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    hora1 = datetime.strptime(hora1_str, "%H:%M").time()
    hora2 = datetime.strptime(hora2_str, "%H:%M").time()
    hora1_utc = (datetime.combine(datetime.today(), hora1) + timedelta(hours=3)).time()
    hora2_utc = (datetime.combine(datetime.today(), hora2) + timedelta(hours=3)).time()
    job_queue.run_daily(chequeo_registro_individual_mediodia, time=hora1_utc, chat_id=chat_id, name=f"mediodia:{chat_id}")
    job_queue.run_daily(chequeo_registro_individual_noche, time=hora2_utc, chat_id=chat_id, name=f"noche:{chat_id}")


async def horarios_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        hora1_str, hora2_str = context.args[0], context.args[1]
        datetime.strptime(hora1_str, "%H:%M")
        datetime.strptime(hora2_str, "%H:%M")
    except (IndexError, ValueError):
        await update.message.reply_text("⚠️ Uso correcto: /horarios HH:MM HH:MM (ej: /horarios 13:30 21:00) — primero mediodía, después noche.")
        return

    user_id = str(update.effective_user.id)
    chat_id = update.effective_chat.id
    profiles = get_db(PROFILES_FILE)
    perfil = profiles.setdefault(user_id, {})
    perfil["horario_personalizado"] = True
    perfil["hora_mediodia"] = hora1_str
    perfil["hora_noche"] = hora2_str
    save_db(PROFILES_FILE, profiles)

    _programar_jobs_horario(context.application.job_queue, chat_id, hora1_str, hora2_str)

    await update.message.reply_text(
        f"⏰ Listo, tus recordatorios personalizados quedaron en {hora1_str} y {hora2_str} (hora Argentina). "
        f"Dejaste de recibir los horarios genéricos de las 14/23hs."
    )


# --- SISTEMA ANTI-BUCLES Y PROCESAMIENTO ---
async def _procesar_y_pedir_confirmacion(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                          text_input: str, file_bytes, mime_type, media_label,
                                          target_date: str):
    message = update.message
    user_id = str(update.effective_user.id)
    profiles = get_db(PROFILES_FILE)
    perfil = profiles.get(user_id, {})
    peso_usuario = perfil.get("peso", 75.0)
    deporte_preferido = perfil.get("deporte", "Squash")

    service: GeminiNutritionService = context.application.bot_data["nutrition_service"]
    req = GeminiInput(text=text_input, media_bytes=file_bytes, mime_type=mime_type, media_label=media_label)

    try:
        ai_response = await service.analyze(req)
        
        # Filtro Anti-Bucle: Si la IA te interroga en vez de resolver, forzamos el modo offline
        if "describime cantidades" in ai_response.lower() or "faltan datos" in ai_response.lower():
            raise ValueError("Gemini entró en bucle pidiendo aclaraciones.")

        datos = extraer_datos_estructurados(ai_response, peso_usuario=peso_usuario, deporte_preferido=deporte_preferido)
        clean_response = limpiar_respuesta(ai_response)

        context.user_data["pending_analysis"] = clean_response
        context.user_data["pending_tipo"] = datos["tipo"]
        context.user_data["pending_tip"] = datos["tip_medico"]
        context.user_data["pending_kcal"] = datos["kcal"]
        context.user_data["pending_macros"] = {
            "proteinas": datos["proteinas"], "carbohidratos": datos["carbohidratos"], "grasas": datos["grasas"],
        }
        context.user_data["pending_date"] = target_date

        prefijo_fecha = "" if target_date == get_fecha_argentina() else f"🗓️ *(Se registrará con fecha {target_date})*\n\n"

        await message.reply_text(
            f"{prefijo_fecha}📋 *Análisis:*\n\n{clean_response}\n\n¿Registramos esto?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar", callback_data="confirm"), InlineKeyboardButton("✏️ Editar", callback_data="edit")],
                [InlineKeyboardButton("👨‍⚕️ Tip Médico", callback_data="med_tip")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error Gemini o Bucle detectado: {e}")
        
        # FALLBACK OFFLINE INTELIGENTE
        datos_locales = extraer_datos_estructurados(text_input, peso_usuario=peso_usuario, deporte_preferido=deporte_preferido)
        
        if datos_locales["kcal"] > 0:
            context.user_data["pending_analysis"] = f"⚡ *Registro Rápido (Modo Offline)*\nProcesado directamente de tu texto: _{text_input}_"
            context.user_data["pending_tipo"] = datos_locales["tipo"]
            context.user_data["pending_tip"] = "Trata de mantener un buen balance de hidratación."
            context.user_data["pending_kcal"] = datos_locales["kcal"]
            context.user_data["pending_macros"] = {
                "proteinas": datos_locales["proteinas"], "carbohidratos": datos_locales["carbohidratos"], "grasas": datos_locales["grasas"],
            }
            context.user_data["pending_date"] = target_date

            prefijo_fecha = "" if target_date == get_fecha_argentina() else f"🗓️ *(Se registrará con fecha {target_date})*\n\n"

            await message.reply_text(
                f"{prefijo_fecha}⚠️ *Problemas con la IA*, pero extraje los datos directamente de tu mensaje.\n\n"
                f"Detecté **{datos_locales['kcal']} kcal** ({datos_locales['tipo'].replace('_', ' ')}).\n¿Lo guardamos para no perder la racha?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Guardar igual", callback_data="confirm"), InlineKeyboardButton("✏️ Editar", callback_data="edit")]
                ]),
                parse_mode="Markdown"
            )
        else:
            await message.reply_text(
                "¡Entendido, colega! Procesé la comida/entrenamiento, pero la IA está saturada y no pude estimar las calorías exactas automáticamente.\n\n"
                "Para no trabarnos, **reescribí el mensaje poniéndole el número de calorías estimado al final** (ejemplo: *'fideos con verduras, 450 kcal'*). ¡Así lo guardo directo! 💪",
                parse_mode="Markdown"
            )


async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    text_input = message.text or message.caption or ""

    if len(text_input.strip()) < 3 and not message.photo and not message.voice and not message.audio:
        await message.reply_text("👍")
        return

    if context.user_data.get("esperando_edicion"):
        context.user_data["esperando_edicion"] = False
        text_input = f"Corrección sobre el registro anterior: {text_input}"

    settings = context.application.bot_data["settings"]
    max_bytes = settings.max_media_bytes
    file_bytes, mime_type, media_label = None, None, None

    if message.photo:
        photo = message.photo[-1]
        if photo.file_size and photo.file_size > max_bytes:
            await message.reply_text(f"⚠️ La imagen es demasiado grande (máx {max_bytes // 1_000_000} MB).")
            return
        await message.reply_text("👀 Analizando la imagen... Un momento.")
        photo_file = await photo.get_file()
        file_bytes = bytes(await photo_file.download_as_bytearray())
        if len(file_bytes) > max_bytes:
            await message.reply_text(f"⚠️ La imagen superó el límite de {max_bytes // 1_000_000} MB.")
            return
        mime_type = "image/jpeg"
        media_label = "fotografía de alimentos o bebidas"
        if not text_input: text_input = "Analiza los alimentos o bebidas de esta imagen y calcula las calorías y macronutrientes."

    elif message.voice or message.audio:
        voice_or_audio = message.voice or message.audio
        if voice_or_audio.file_size and voice_or_audio.file_size > max_bytes:
            await message.reply_text(f"⚠️ El audio es demasiado pesado (máx {max_bytes // 1_000_000} MB).")
            return
        await message.reply_text("🎧 Escuchando tu nota de voz...")
        voice_file = await voice_or_audio.get_file()
        file_bytes = bytes(await voice_file.download_as_bytearray())
        if len(file_bytes) > max_bytes:
            await message.reply_text(f"⚠️ El audio superó el límite de {max_bytes // 1_000_000} MB.")
            return
        mime_type = "audio/ogg"
        media_label = "nota de voz del usuario"
        text_input = text_input or "Procesa esta nota de voz sobre mi ingesta o actividad física."

    await _procesar_y_pedir_confirmacion(update, context, text_input, file_bytes, mime_type, media_label, target_date=get_fecha_argentina())


async def ayer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = " ".join(context.args).strip()
    if not texto:
        await update.message.reply_text("⚠️ Uso correcto: /ayer <lo que comiste o entrenaste>\nEj: /ayer almorcé milanesa con puré")
        return
    await update.message.reply_text("🔍 Analizando tu registro de ayer...")
    await _procesar_y_pedir_confirmacion(update, context, texto, None, None, None, target_date=get_fecha_ayer_argentina())


def _registrar_en_log(logs, user_id, fecha, tipo, kcal, macros):
    if user_id not in logs: logs[user_id] = {}
    if fecha not in logs[user_id]: logs[user_id][fecha] = _dia_vacio()
    dia = logs[user_id][fecha]

    if tipo == "gasto_cardio":
        dia["kcal_quemadas_cardio"] = dia.get("kcal_quemadas_cardio", 0) + kcal
        dia["kcal_quemadas"] = dia.get("kcal_quemadas", 0) + kcal
        tipo_msj = f"🏃‍♂️ {kcal} kcal de cardio registradas"
    elif tipo == "gasto_fuerza":
        dia["kcal_quemadas_fuerza"] = dia.get("kcal_quemadas_fuerza", 0) + kcal
        dia["kcal_quemadas"] = dia.get("kcal_quemadas", 0) + kcal
        tipo_msj = f"🏋️‍♂️ {kcal} kcal de fuerza registradas"
    else:
        dia["kcal_ing"] = dia.get("kcal_ing", 0) + kcal
        dia["proteinas"] = dia.get("proteinas", 0.0) + macros.get("proteinas", 0.0)
        dia["carbohidratos"] = dia.get("carbohidratos", 0.0) + macros.get("carbohidratos", 0.0)
        dia["grasas"] = dia.get("grasas", 0.0) + macros.get("grasas", 0.0)
        tipo_msj = f"🍽️ {kcal} kcal ingeridas registradas"

    dia["last_action"] = {"tipo": tipo, "kcal": kcal, "macros": macros}
    return dia, tipo_msj


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm":
        analisis = context.user_data.pop("pending_analysis", "")
        tipo = context.user_data.pop("pending_tipo", "ingesta")
        kcal_detectadas = context.user_data.pop("pending_kcal", None)
        macros = context.user_data.pop("pending_macros", None)
        fecha_destino = context.user_data.pop("pending_date", get_fecha_argentina())
        user_id = str(query.from_user.id)

        if kcal_detectadas is None:
            kcal_detectadas = extraer_calorias(analisis)
        if macros is None:
            macros = extraer_macros(analisis)

        logs = get_db(LOGS_FILE)
        dia, tipo_msj = _registrar_en_log(logs, user_id, fecha_destino, tipo, kcal_detectadas, macros)

        profiles = get_db(PROFILES_FILE)
        perfil = profiles.setdefault(user_id, {})
        if tipo == "ingesta":
            actualizar_comidas_frecuentes(perfil, analisis, kcal_detectadas, macros)
            save_db(PROFILES_FILE, profiles)

        refuerzo = verificar_y_generar_refuerzo_superavit(perfil, dia)
        save_db(LOGS_FILE, logs)

        sufijo_fecha = "" if fecha_destino == get_fecha_argentina() else f" (fecha: {fecha_destino})"
        texto_final = f"{analisis}\n\n✅ *{tipo_msj}{sufijo_fecha}.* 🚀"
        if refuerzo:
            texto_final += f"\n\n{refuerzo}"
        await query.edit_message_text(texto_final, parse_mode="Markdown")

    elif query.data.startswith("quick:"):
        idx = int(query.data.split(":")[1])
        opciones = context.user_data.get("quick_options", [])
        if idx >= len(opciones):
            await query.edit_message_text("Esa opción ya no está disponible, probá /rapido de nuevo.")
            return
        opcion = opciones[idx]
        user_id = str(query.from_user.id)
        today = get_fecha_argentina()

        logs = get_db(LOGS_FILE)
        dia, tipo_msj = _registrar_en_log(logs, user_id, today, "ingesta", opcion["kcal"], opcion["macros"])

        profiles = get_db(PROFILES_FILE)
        perfil = profiles.setdefault(user_id, {})
        refuerzo = verificar_y_generar_refuerzo_superavit(perfil, dia)
        save_db(LOGS_FILE, logs)

        texto_final = f"✅ *{tipo_msj}* ({opcion['texto']}) 🚀"
        if refuerzo:
            texto_final += f"\n\n{refuerzo}"
        await query.edit_message_text(texto_final, parse_mode="Markdown")

    elif query.data == "edit":
        context.user_data["esperando_edicion"] = True
        await query.edit_message_text("✏️ Modo edición. Escribime la descripción corregida:")

    elif query.data == "med_tip":
        tip_text = context.user_data.get("pending_tip", "Consulta siempre a tu profesional de cabecera.")
        if random.random() < 0.4:
            tip_text += "\n\n" + random.choice(FRASES_MOTIVACION_EXTRA)
        await query.message.reply_text(f"👨‍⚕️ *Perspectiva Médica y Metabólica:*\n\n{tip_text}", parse_mode="Markdown")

    elif query.data == "download_report":
        await reporte_command(update, context)


# --- REPORTES Y BALANCE ---

async def balance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    today = get_fecha_argentina()

    profiles = get_db(PROFILES_FILE)
    logs = get_db(LOGS_FILE)

    user_profile = profiles.get(user_id, {})
    user_log = logs.get(user_id, {}).get(today, _dia_vacio())

    kcal_objetivo = user_log.get("objetivo_temporal", user_profile.get("kcal_objetivo", 2200))
    kcal_ing = user_log.get("kcal_ing", 0)
    kcal_quemadas = user_log.get("kcal_quemadas", 0)
    balance_diario = kcal_ing - kcal_quemadas
    kcal_restantes = kcal_objetivo - balance_diario

    proteinas = user_log.get("proteinas", 0.0)
    carbohidratos = user_log.get("carbohidratos", 0.0)
    grasas = user_log.get("grasas", 0.0)
    objetivo_proteina = user_profile.get("objetivo_proteina_g")
    linea_proteina_obj = f" (objetivo: {objetivo_proteina}g)" if objetivo_proteina else ""

    await update.message.reply_text(
        f"📊 *Balance Diario ({today})*\n\n"
        f"• *Objetivo de hoy:* {kcal_objetivo} kcal\n"
        f"• *Ingeridas:* {kcal_ing} kcal\n"
        f"• *Quemadas por ejercicio:* {kcal_quemadas} kcal\n"
        f"• *Macros:* {proteinas:.0f}g P{linea_proteina_obj}, {carbohidratos:.0f}g C, {grasas:.0f}g G\n\n"
        f"⚖️ *Balance neto (Ingeridas - Quemadas):* {balance_diario:+d} kcal\n"
        f"📉 *Faltan para tu objetivo:* {kcal_restantes} kcal",
        parse_mode="Markdown"
    )

async def balance_general_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    profiles = get_db(PROFILES_FILE)
    logs_user = get_db(LOGS_FILE).get(user_id, {})

    if not logs_user:
        await update.message.reply_text("Todavía no hay registros históricos cargados.")
        return

    kcal_objetivo_base = profiles.get(user_id, {}).get("kcal_objetivo", 2200)
    total_dias = len(logs_user)
    suma_ing = sum(d.get("kcal_ing", 0) for d in logs_user.values())
    suma_quem = sum(d.get("kcal_quemadas", 0) for d in logs_user.values())
    suma_prot = sum(d.get("proteinas", 0.0) for d in logs_user.values())
    suma_carb = sum(d.get("carbohidratos", 0.0) for d in logs_user.values())
    suma_gras = sum(d.get("grasas", 0.0) for d in logs_user.values())

    promedio_ing = int(suma_ing / total_dias) if total_dias > 0 else 0
    promedio_quem = int(suma_quem / total_dias) if total_dias > 0 else 0
    promedio_neto = promedio_ing - promedio_quem
    promedio_prot = suma_prot / total_dias if total_dias > 0 else 0
    promedio_carb = suma_carb / total_dias if total_dias > 0 else 0
    promedio_gras = suma_gras / total_dias if total_dias > 0 else 0

    evaluacion = "🟢 En rango óptimo respecto al objetivo"
    if promedio_ing < kcal_objetivo_base - 200:
        evaluacion = "📉 Por debajo de la meta (Déficit acentuado)"
    elif promedio_ing > kcal_objetivo_base + 200:
        evaluacion = "📈 Por encima de la meta (Superávit)"

    await update.message.reply_text(
        f"📈 *Balance General y Promedios ({total_dias} días registrados)*\n\n"
        f"• *Promedio Ingerido:* {promedio_ing} kcal/día\n"
        f"• *Objetivo Diario Base:* {kcal_objetivo_base} kcal/día\n"
        f"• *Promedio Quemado:* {promedio_quem} kcal/día\n"
        f"• *Promedio Macros:* {promedio_prot:.0f}g P, {promedio_carb:.0f}g C, {promedio_gras:.0f}g G\n"
        f"⚖️ *Balance Neto Promedio:* {promedio_neto:+d} kcal/día\n\n"
        f"💡 *Evaluación:* {evaluacion}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📄 Descargar Reporte en Excel", callback_data="download_report")]]),
        parse_mode="Markdown"
    )

async def reporte_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    logs_user = get_db(LOGS_FILE).get(user_id, {})
    message = update.effective_message

    if not logs_user:
        await message.reply_text("Todavía no hay registros para exportar.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Fecha", "Kcal Ingeridas", "Quemadas (Cardio)", "Quemadas (Fuerza)",
                      "Total Quemadas", "Balance Neto", "Proteínas (g)", "Carbohidratos (g)", "Grasas (g)"])

    for fecha, datos in sorted(logs_user.items()):
        ing = datos.get("kcal_ing", 0)
        cardio = datos.get("kcal_quemadas_cardio", 0)
        fuerza = datos.get("kcal_quemadas_fuerza", 0)
        total_quemadas = datos.get("kcal_quemadas", 0)
        balance = ing - total_quemadas
        writer.writerow([fecha, ing, cardio, fuerza, total_quemadas, balance,
                          round(datos.get("proteinas", 0.0), 1), round(datos.get("carbohidratos", 0.0), 1), round(datos.get("grasas", 0.0), 1)])

    csv_bytes = io.BytesIO(output.getvalue().encode('utf-8'))
    fecha_archivo = get_fecha_argentina().replace("-", "")
    csv_bytes.name = f"reporte_metabolico_{fecha_archivo}.csv"

    await message.reply_document(document=csv_bytes, caption="📊 Aquí tenés tu reporte con promedios, macros y desglose de Cardio y Fuerza.")


# --- NOTIFICACIONES AUTOMÁTICAS (JOB QUEUE) ---

async def verificar_registros_14hs(context: ContextTypes.DEFAULT_TYPE):
    today = get_fecha_argentina()
    logs = get_db(LOGS_FILE)
    profiles = get_db(PROFILES_FILE)
    for user_id, user_logs in logs.items():
        if profiles.get(user_id, {}).get("horario_personalizado"):
            continue
        dia = user_logs.setdefault(today, _dia_vacio())
        kcal_ing = dia.get("kcal_ing", 0)
        kcal_quemadas = dia.get("kcal_quemadas", 0)
        dia["checkpoint_14hs"] = {"kcal_ing": kcal_ing, "kcal_quemadas": kcal_quemadas}
        if kcal_ing == 0 and kcal_quemadas == 0:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=("⏱️ *Recordatorio de Media Jornada*\n\n¡Hola! Aún no has registrado actividad y/o alimento el día de hoy. "
                          "No olvides hacerlo para tener un correcto análisis mensual. 🥑"),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"No se pudo enviar recordatorio de 14hs a {user_id}: {e}")
    save_db(LOGS_FILE, logs)

async def verificar_registros_23hs(context: ContextTypes.DEFAULT_TYPE):
    today = get_fecha_argentina()
    logs = get_db(LOGS_FILE)
    profiles = get_db(PROFILES_FILE)
    for user_id, user_logs in logs.items():
        if profiles.get(user_id, {}).get("horario_personalizado"):
            continue
        dia = user_logs.get(today, {})
        kcal_ing = dia.get("kcal_ing", 0)
        kcal_quemadas = dia.get("kcal_quemadas", 0)
        checkpoint = dia.get("checkpoint_14hs")
        if checkpoint is not None:
            sin_novedades = (kcal_ing == checkpoint.get("kcal_ing", 0) and kcal_quemadas == checkpoint.get("kcal_quemadas", 0))
        else:
            sin_novedades = (kcal_ing == 0 and kcal_quemadas == 0)
        if sin_novedades:
            try:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=("🌙 *Cierre de Jornada*\n\nColega, aún no has registrado actividad y/o alimento el día de hoy. "
                          "No olvides hacerlo para tener un correcto análisis mensual. ¡A descansar!"),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"No se pudo enviar recordatorio nocturno a {user_id}: {e}")

# --- CORRECCIÓN LÓGICA DE COMODINES Y RACHA ---
async def verificar_racha_diaria(context: ContextTypes.DEFAULT_TYPE):
    ayer = get_fecha_ayer_argentina()
    profiles = get_db(PROFILES_FILE)
    logs = get_db(LOGS_FILE)
    lunes_actual = _lunes_de_semana(get_fecha_argentina())
    cambios = False

    for user_id, perfil in profiles.items():
        dia_ayer = logs.get(user_id, {}).get(ayer, {})
        tuvo_actividad = dia_ayer.get("kcal_ing", 0) > 0 or dia_ayer.get("kcal_quemadas", 0) > 0

        # 1. Evaluar si cumplió ayer y actualizar la racha O restar un comodín de la semana vieja
        if tuvo_actividad:
            perfil["racha_actual"] = perfil.get("racha_actual", 0) + 1
            cambios = True
        else:
            comodines = perfil.get("comodines", COMODINES_POR_SEMANA)
            if comodines > 0:
                perfil["comodines"] = comodines - 1
                cambios = True
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🧊 Usaste un comodín para no perder tu racha (te quedan {perfil['comodines']}). Racha: {perfil.get('racha_actual', 0)} días 🔥",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"No se pudo avisar comodín a {user_id}: {e}")
            else:
                if perfil.get("racha_actual", 0) > 0:
                    try:
                        await context.bot.send_message(chat_id=user_id, text="Tu racha se reinició. Arrancamos de nuevo hoy — lo importante es volver. 💪")
                    except Exception as e:
                        logger.error(f"No se pudo avisar corte de racha a {user_id}: {e}")
                perfil["racha_actual"] = 0
                cambios = True

        # 2. Después de evaluar el domingo, SI ES LUNES, reseteamos comodines a la semana nueva
        if perfil.get("semana_comodines") != lunes_actual:
            perfil["semana_comodines"] = lunes_actual
            perfil["comodines"] = COMODINES_POR_SEMANA
            cambios = True

    if cambios:
        save_db(PROFILES_FILE, profiles)


async def resumen_semanal_job(context: ContextTypes.DEFAULT_TYPE):
    profiles = get_db(PROFILES_FILE)
    logs = get_db(LOGS_FILE)
    hoy = datetime.now(ARG_TZ).date()
    fechas_semana = [(hoy - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]

    for user_id, perfil in profiles.items():
        logs_usuario = logs.get(user_id, {})
        valores_ing = [logs_usuario.get(f, {}).get("kcal_ing", 0) for f in fechas_semana]
        if not any(valores_ing):
            continue

        objetivo = perfil.get("kcal_objetivo", 2200)
        dias_label = [datetime.strptime(f, "%Y-%m-%d").strftime("%a %d") for f in fechas_semana]

        fig, ax = plt.subplots(figsize=(6, 3.5))
        ax.bar(dias_label, valores_ing, color="#4FA88A", label="Ingeridas")
        ax.axhline(objetivo, color="#E8871E", linestyle="--", label="Objetivo")
        ax.set_ylabel("kcal")
        ax.legend()
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        buf.seek(0)

        promedio = sum(valores_ing) / 7
        try:
            await context.bot.send_photo(
                chat_id=user_id, photo=buf,
                caption=f"📅 *Tu semana:* promedio {int(promedio)} kcal/día vs objetivo {objetivo} kcal.\n¡Seguí así! 💪",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"No se pudo enviar resumen semanal a {user_id}: {e}")


def _reprogramar_horarios_personalizados(application):
    profiles = get_db(PROFILES_FILE)
    for user_id, perfil in profiles.items():
        if perfil.get("horario_personalizado") and perfil.get("hora_mediodia") and perfil.get("hora_noche"):
            try:
                _programar_jobs_horario(application.job_queue, int(user_id), perfil["hora_mediodia"], perfil["hora_noche"])
            except Exception as e:
                logger.error(f"No se pudo reprogramar horario personalizado de {user_id}: {e}")


def build_application():
    settings = get_settings()
    application = Application.builder().token(settings.telegram_bot_token).build()
    application.bot_data["nutrition_service"] = GeminiNutritionService(settings)
    application.bot_data["settings"] = settings

    job_queue = application.job_queue
    job_queue.run_daily(verificar_registros_14hs, time=time(hour=17, minute=0))
    job_queue.run_daily(verificar_registros_23hs, time=time(hour=2, minute=0))
    job_queue.run_daily(verificar_racha_diaria, time=time(hour=3, minute=5))
    job_queue.run_daily(resumen_semanal_job, time=time(hour=23, minute=0), days=(6,))

    _reprogramar_horarios_personalizados(application)

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
    application.add_handler(CommandHandler("help", guia_command))
    application.add_handler(CommandHandler("guia", guia_command))
    application.add_handler(CommandHandler("setobjetivo", set_objetivo_command))
    application.add_handler(CommandHandler("peso", peso_command))
    application.add_handler(CommandHandler("partidomanana", partidomanana_command))
    application.add_handler(CommandHandler("quecomo", quecomo_command))
    application.add_handler(CommandHandler("ayer", ayer_command))
    application.add_handler(CommandHandler("rapido", rapido_command))
    application.add_handler(CommandHandler("racha", racha_command))
    application.add_handler(CommandHandler("horarios", horarios_command))
    application.add_handler(CommandHandler("eliminarultimo", eliminar_ultimo_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("balancegeneral", balance_general_command))
    application.add_handler(CommandHandler("reporte", reporte_command))

    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO | filters.VOICE | filters.AUDIO) & ~filters.COMMAND, handle_input))
    application.add_handler(CallbackQueryHandler(button_callback))

    return application

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    logger.info("Servidor web Flask iniciado.")
    app_bot = build_application()
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES)
