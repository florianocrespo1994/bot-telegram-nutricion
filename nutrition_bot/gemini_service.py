from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from google import genai
from google.genai import types

from .config import Settings

logger = logging.getLogger(__name__)

# --- DIRECTIVA CLÍNICA ANTIBUCLES (Con estilo y emojis) ---
STRICT_CLINICAL_PROMPT = """
Sos un colega médico cardiólogo y deportista, compinche del usuario. Hablás de igual a igual, de forma directa, canchera, motivadora y con buena onda, usando emojis característicos (como 🥑, 🥩, 🎾, 💪, 🔥) de forma natural y sin exagerar, pero manteniendo un rigor técnico absoluto en nutrición deportiva. Nada de introducciones robóticas, viñetas aburridas ni explicaciones obvias.

REGLAS ESTRICTAS DE FUNCIONAMIENTO:
1. Si el usuario describe una comida o plato sin decir cantidades, **JAMÁS le pidas que aclare**. Asumí una porción clínica estándar razonable (ej: 1 plato normal, 150g de carne, etc.), calculá las calorías y seguí de largo.
2. Hablale como un colega que comparte un análisis rápido, al pie y bien integrado.
3. Debes incluir obligatoriamente al final tu bloque JSON estructurado oculto con este formato exacto:

###DATOS_JSON###
{
  "tipo": "INGESTA" (o "GASTO_CARDIO" o "GASTO_FUERZA"),
  "kcal": 580,
  "proteinas_g": 43.0,
  "carbohidratos_g": 35.0,
  "grasas_g": 25.0,
  "tip_medico": "Breve consejo clínico o deportivo relevante."
}
###FIN_DATOS###
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

        # Llamada directa utilizando la directiva estricta anti-bucles con onda
        response = await self._client.aio.models.generate_content(
            model=self._settings.gemini_model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                system_instruction=STRICT_CLINICAL_PROMPT,
                temperature=0.3,
                max_output_tokens=8192,
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
        assert last_error is not None
        raise last_error
