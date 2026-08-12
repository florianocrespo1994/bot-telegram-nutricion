from __future__ import annotations

import logging
from datetime import datetime, time

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from .config import Settings
from .gemini_service import GeminiInput, GeminiNutritionService
from .prompts import HELP_MESSAGE, WELCOME_MESSAGE


logger = logging.getLogger(__name__)

# Almacén temporal de perfiles en memoria compartido
user_profiles: dict[int, str] = {}


def _service(context: ContextTypes.DEFAULT_TYPE) -> GeminiNutritionService:
    return context.application.bot_data["nutrition_service"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message:
        await update.message.reply_text(WELCOME_MESSAGE)


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if update.message:
        await update.message.reply_text(HELP_MESSAGE)


async def handle_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.message.text or not update.effective_user:
        return

    text = update.message.text
    user_id = update.effective_user.id
    text_lower = text.lower()

    # Detectar y guardar perfil
    if any(k in text_lower for k in ["edad", "peso", "altura", "cm", "kg", "sexo", "objetivo", "masculino", "femenino", "mantenimiento", "déficit", "superávit"]):
        user_profiles[user_id] = text
        await update.message.reply_text(
            "¡Perfil recibido y guardado correctamente! He registrado tus datos y tu objetivo calórico diario. "
            "Ya puedes comenzar a registrar tus 4 comidas (Desayuno, Almuerzo, Merienda, Cena) o tu actividad física."
        )
        return

    # Adjuntar perfil al texto si existe
    profile = user_profiles.get(user_id, "")
    full_text = f"Datos del paciente: {profile}\n\nEntrada del usuario: {text}" if profile else text

    await _analyze_and_reply(
        update,
        context,
        GeminiInput(text=full_text),
    )


async def handle_media(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_user:
        return

    message = update.message
    user_id = update.effective_user.id
    telegram_file = None
    mime_type = None
    label = None

    if message.photo:
        photo = message.photo[-1]
        telegram_file = await photo.get_file()
        mime_type = "image/jpeg"
        label = "fotografía de comida o actividad"
    elif message.voice:
        telegram_file = await message.voice.get_file()
        mime_type = message.voice.mime_type or "audio/ogg"
        label = "nota de voz"
    elif message.audio:
        telegram_file = await message.audio.get_file()
        mime_type = message.audio.mime_type or "audio/mpeg"
        label = "archivo de audio"

    if telegram_file is None or mime_type is None:
        return

    expected_size = getattr(message.photo[-1], "file_size", None) if message.photo else (
        message.voice.file_size if message.voice else message.audio.file_size
    )
    max_media_bytes = _settings(context).max_media_bytes
    if expected_size and expected_size > max_media_bytes:
        await message.reply_text(
            "El archivo es demasiado grande para analizarlo. "
            f"Envíalo con un tamaño menor a {max_media_bytes // 1_000_000} MB."
        )
        return

    media_bytes = bytes(await telegram_file.download_as_bytearray())
    if len(media_bytes) > max_media_bytes:
        await message.reply_text(
            "El archivo es demasiado grande para analizarlo. "
            f"Envíalo con un tamaño menor a {max_media_bytes // 1_000_000} MB."
        )
        return

    caption = message.caption.strip() if message.caption else ""
    profile = user_profiles.get(user_id, "")
    full_caption = f"Datos del paciente: {profile}\n\nEntrada del usuario: {caption}" if profile else caption

    await _analyze_and_reply(
        update,
        context,
        GeminiInput(
            text=full_caption if full_caption else None,
            media_bytes=media_bytes,
            mime_type=mime_type,
            media_label=label,
        ),
    )


async def _analyze_and_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    request: GeminiInput,
) -> None:
    if not update.message:
        return
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id if update.effective_chat else update.message.chat_id,
        action=ChatAction.TYPING,
    )
    try:
        answer = await _service(context).analyze_with_retry(request)
        await update.message.reply_text(answer)
    except Exception as e:
        logger.exception("Nutrition analysis failed: %s", e)
        await update.message.reply_text(
            "No pude analizarlo esta vez. Inténtalo de nuevo en unos segundos."
        )


async def reminder_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_chat:
        return
    if context.job_queue is None:
        await update.message.reply_text(
            "Los recordatorios no están disponibles en esta configuración."
        )
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Formato: /recordatorio HH:MM mensaje\n"
            "Ejemplo: /recordatorio 20:00 Recuerda registrar tu actividad física del día"
        )
        return

    try:
        reminder_time = datetime.strptime(context.args[0], "%H:%M").time()
    except ValueError:
        await update.message.reply_text(
            "La hora debe tener formato HH:MM, por ejemplo 20:00."
        )
        return

    reminder_text = " ".join(context.args[1:]).strip()
    job_name = f"nutrition-reminder:{update.effective_chat.id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    context.job_queue.run_daily(
        send_scheduled_reminder,
        time=reminder_time,
        chat_id=update.effective_chat.id,
        name=job_name,
        data={"text": reminder_text},
    )
    timezone_name = _settings(context).timezone.key
    await update.message.reply_text(
        f"Listo. Te enviaré este recordatorio todos los días a las "
        f"{reminder_time.strftime('%H:%M')} ({timezone_name}):\n{reminder_text}"
    )


async def cancel_reminder_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    if not update.message or not update.effective_chat:
        return
    if context.job_queue is None:
        return
    job_name = f"nutrition-reminder:{update.effective_chat.id}"
    jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in jobs:
        job.schedule_removal()
    if jobs:
        await update.message.reply_text("Tu recordatorio diario fue cancelado.")
    else:
        await update.message.reply_text("No tienes un recordatorio diario activo.")


async def send_scheduled_reminder(
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    job = context.job
    if job is None or job.chat_id is None:
        return
    reminder = job.data["text"] if isinstance(job.data, dict) else str(job.data)
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"Recordatorio nutricional:\n{reminder}",
    )


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    logger.exception("Unhandled Telegram error", exc_info=context.error)
