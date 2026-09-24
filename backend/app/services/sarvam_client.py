"""
Sarvam STT / TTS — ported from sarvam_voice.py, made stateless for FastAPI.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional, Tuple

import requests

from app.config import get_settings

logger = logging.getLogger(__name__)

SARVAM_STT_URL = "https://api.sarvam.ai/speech-to-text"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"


def transcribe(
    audio_bytes: bytes,
    filename: str = "audio.wav",
    content_type: str = "audio/wav",
) -> Tuple[str, Optional[str]]:
    """
    Returns (transcript_text, detected_language).
    """
    settings = get_settings()
    if not settings.sarvam_api_key:
        raise RuntimeError("SARVAM_API_KEY is not configured")

    files = {
        "file": (filename, audio_bytes, content_type),
    }
    data = {
        "model": settings.sarvam_stt_model,
        "mode": settings.sarvam_stt_mode,
    }
    headers = {"api-subscription-key": settings.sarvam_api_key}

    resp = requests.post(
        SARVAM_STT_URL, headers=headers, files=files, data=data, timeout=60
    )
    resp.raise_for_status()
    body = resp.json()
    text = body.get("transcript") or body.get("text") or ""
    lang = body.get("language_code") or body.get("language")
    return text, lang


def synthesize(
    text: str,
    language: Optional[str] = None,
    speaker: Optional[str] = None,
) -> bytes:
    """
    Returns raw audio bytes (wav / mp3 depending on Sarvam response).
    """
    settings = get_settings()
    if not settings.sarvam_api_key:
        raise RuntimeError("SARVAM_API_KEY is not configured")

    payload = {
        "text": text,
        "target_language_code": language or settings.sarvam_tts_language,
        "speaker": speaker or settings.sarvam_tts_speaker,
        "model": settings.sarvam_tts_model,
        "pace": settings.sarvam_tts_pace,
    }
    headers = {
        "api-subscription-key": settings.sarvam_api_key,
        "Content-Type": "application/json",
    }
    resp = requests.post(SARVAM_TTS_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    # Sarvam returns base64-encoded audio in "audios" list
    audios = body.get("audios") or []
    if not audios:
        raise RuntimeError(f"Sarvam TTS returned no audio: {body}")
    return base64.b64decode(audios[0])
