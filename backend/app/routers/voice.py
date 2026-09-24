"""
Voice proxy routes — frontend calls these around /agent/chat.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response

from app.deps import get_current_user
from app.schemas.voice import SynthesizeRequest, TranscribeResponse
from app.services import sarvam_client

router = APIRouter(prefix="/voice", tags=["voice"])


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe(
    file: UploadFile = File(...),
    user: Dict[str, Any] = Depends(get_current_user),
) -> TranscribeResponse:
    raw = await file.read()
    content_type = file.content_type or "audio/wav"
    # Normalise browser quirks
    if "wave" in content_type or content_type == "audio/vnd.wave":
        content_type = "audio/wav"
    try:
        text, lang = sarvam_client.transcribe(
            raw, filename=file.filename or "audio.wav", content_type=content_type
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"STT failed: {e}")
    return TranscribeResponse(text=text, language=lang)


@router.post("/synthesize")
def synthesize(
    body: SynthesizeRequest,
    user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    try:
        audio = sarvam_client.synthesize(
            body.text, language=body.language, speaker=body.speaker
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"TTS failed: {e}")
    return Response(content=audio, media_type="audio/wav")
