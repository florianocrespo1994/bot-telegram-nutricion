from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str | None
    gemini_api_key: str
    gemini_model: str
    timezone: ZoneInfo
    max_media_bytes: int


def get_settings() -> Settings:
    gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not gemini_api_key:
        raise RuntimeError(
            "Falta GEMINI_API_KEY. Guárdala como secreto antes de iniciar el bot."
        )

    timezone_name = os.getenv("BOT_TIMEZONE", "America/Mexico_City").strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception as exc:
        raise RuntimeError(
            f"BOT_TIMEZONE no es válido: {timezone_name!r}"
        ) from exc

    try:
        max_media_bytes = int(os.getenv("MAX_MEDIA_BYTES", "8000000"))
    except ValueError as exc:
        raise RuntimeError("MAX_MEDIA_BYTES debe ser un número entero.") from exc

    return Settings(
        telegram_bot_token=(
            os.getenv("TELEGRAM_TOKEN", "").strip()
            or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
            or None
        ),
        gemini_api_key=gemini_api_key,
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3-flash-preview").strip(),
        timezone=timezone,
        max_media_bytes=max_media_bytes,
    )