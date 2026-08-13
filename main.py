# --- LÓGICA DEL BOT (Optimización conversacional) ---

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text_input = update.message.text or update.message.caption
    
    # 1. Blindaje: Si es muy corto, no llames a Gemini todavía, pregunta detalles
    if len(text_input) < 15:
        await update.message.reply_text(
            "¡Qué bien! Contame un poco más así puedo registrarlo mejor. "
            "¿Qué comiste específicamente o qué ejercicio hiciste y cuánto tiempo duró? 🎾🍎"
        )
        return

    service: GeminiNutritionService = context.application.bot_data["nutrition_service"]
    req = GeminiInput(text=text_input)

    try:
        ai_response = await service.analyze(req)
        context.user_data["pending_analysis"] = ai_response

        # 2. IA Conversacional: Gemini ahora te va a preguntar cosas si falta info
        await update.message.reply_text(
            f"{ai_response}\n\n¿Registramos esto en tu balance diario?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirmar", callback_data="confirm"), 
                 InlineKeyboardButton("✏️ Editar", callback_data="edit")],
                [InlineKeyboardButton("👨‍⚕️ Más Info / Tip Médico", callback_data="med_tip")]
            ]),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error en IA: {e}")
        await update.message.reply_text("¡Qué buena energía! Estoy procesando tu actividad, dame un segundo...")
