from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from google import genai
from google.genai import types

from .config import Settings

logger = logging.getLogger(__name__)

# --- DIRECTIVA NUTRICIONAL ESTRICTA Y RÁPIDA ---
SYSTEM_PROMPT = """
Eres un asistente de registro calórico rápido, preciso y automático. 
NO eres médico. NUNCA uses las palabras "colega", "doctor", "paciente" ni uses jerga clínica. Habla de forma natural, directa y servicial.

REGLAS ESTRICTAS DE FUNCIONAMIENTO (¡MUY IMPORTANTE!):
1. OBLIGACIÓN ABSOLUTA DE CALCULAR: Bajo ninguna circunstancia digas que no puedes calcular, que estás saturado o que te faltan datos. Si el usuario menciona una comida, DEBES estimar las calorías y macronutrientes automáticamente asumiendo una porción estándar. Si no conoces el alimento, haz tu mejor aproximación lógica. NUNCA pongas 0 calorías a un alimento.
2. PRIORIDAD COMIDA VS DEPORTE: Si el usuario menciona una comida (ej. fideos, asado) Y un deporte o actividad en el mismo mensaje, IGNORA la actividad. Trata el mensaje ÚNICAMENTE como una INGESTA de comida. 
3. REGISTRO DE DEPORTE: SOLO calcularás calorías quemadas si el usuario menciona un entrenamiento o deporte SIN mencionar ninguna comida.
4. CERO EXCUSAS Y CERO RELLENO: No pidas disculpas ni justifiques tus cálculos. Ve directo a los números.

FORMATO DE RESPUESTA OBLIGATORIO:
Tu respuesta visible debe ser corta, seguida de tu bloque JSON oculto al final. Usa este formato exacto (reemplazando los corchetes con los valores reales calculados, NO pongas 0 a menos que sea agua):

Anotado: [Nombre del plato o actividad]
🔥 Calorías: [Kcal calculadas] kcal
🥩 Proteínas: [Gramos]g | 🍚 Carbohidratos: [Gramos]g | 🥑 Grasas: [Gramos]g

###DATOS_JSON###
{
  "tipo": "INGESTA", 
  "kcal": [Kcal calculadas],
  "proteinas_g": [Gramos],
  "carbohidratos_g": [Gramos],
  "grasas_g": [Gramos],
  "tip_medico": ""
}
###FIN_DATOS###

NOTAS SOBRE EL JSON: 
- Reemplaza las variables entre corchetes con los NÚMEROS REALES de tu estimación. No escribas los corchetes en tu respuesta.
- En "tipo" debes usar ESTRICTAMENTE uno de estos tres valores: "INGESTA", "GASTO_CARDIO" o "GASTO_FUERZA". 
- Si el tipo es "GASTO_CARDIO" o "GASTO_FUERZA", los valores de proteinas_g, carbohidratos_g y grasas_g deben ser obligatoriamente 0.
- El campo "tip_medico" debe quedar SIEMPRE vacío ("") para evitar dar consejos no solicitados.
"""


@dataclass(frozen=True)
class GeminiInput:
    text: str | None = None
    media_bytes: bytes | None = None
    mime_type: str | None = None
    media_label: str | None = None


class GeminiNutritionService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = genai.Client(api_key=settings.gemini_api_key)

    async def analyze(self, request: GeminiInput) -> str:
        parts: list[types.Part] = []
        if request.text:
            parts.append(types.Part.from_text(text=request.text))
        if request.media_bytes and request.mime_type:
            parts.append(
                types.Part.from_bytes(
                    data=request.media_bytes,
                    mime_type=request.mime_type,
                )
            )
        if not parts:
            raise ValueError("No se recibió contenido para analizar.")

        if request.media_label:
            parts.insert(
                0,
                types.Part.from_text(
                    text=(
                        f"El usuario envió este tipo de contenido: "
                        f"{request.media_label}."
                    )
                ),
            )

        # Llamada directa utilizando la directiva estricta optimizada para velocidad
        response = await self._client.aio.models.generate_content(
            model=self._settings.gemini_model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.1,
                max_output_tokens=300,
            ),
        )
        answer = (response.text or "").strip()
        if not answer:
            raise RuntimeError("Gemini no devolvió una respuesta con texto.")
        return answer

    async def analyze_with_retry(
        self,
        request: GeminiInput,
        attempts: int = 3,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self.analyze(request)
            except Exception as exc:
                last_error = exc
                logger.error("Error detallado de Gemini en intento %s: %s", attempt + 1, exc)
                if attempt == attempts - 1:
                    break
                delay = 2**attempt
                await asyncio.sleep(delay)
        
        logger.error("La API falló tras todos los intentos.")
        return (
            "Anotado: Fallo de conexión\n"
            "🔥 Calorías: 0 kcal\n\n"
            "⚠️ Hubo un error de conexión con la inteligencia artificial. Por favor, intenta enviar tu mensaje de nuevo.\n"
            "###DATOS_JSON###\n"
            '{"tipo": "INGESTA", "kcal": 0, "proteinas_g": 0, "carbohidratos_g": 0, "grasas_g": 0, "tip_medico": ""}\n'
            "###FIN_DATOS###"
        )
