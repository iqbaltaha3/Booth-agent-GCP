from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    question: str
    constituency_id: str
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    answer: str
    charts: List[Dict[str, Any]] = Field(default_factory=list)
    session_id: str
    agent_path: List[str] = Field(default_factory=list)
    latency_ms: Optional[int] = None


class SessionHistoryResponse(BaseModel):
    session_id: str
    turns: List[Dict[str, Any]]
