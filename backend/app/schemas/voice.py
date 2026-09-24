from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class TranscribeResponse(BaseModel):
    text: str
    language: Optional[str] = None


class SynthesizeRequest(BaseModel):
    text: str
    language: Optional[str] = None
    speaker: Optional[str] = None
