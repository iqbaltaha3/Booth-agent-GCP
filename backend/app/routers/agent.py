"""
Main chat endpoint — the only entry the frontend uses for Q&A.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from app.deps import get_current_user, require_constituency
from app.schemas.agent import ChatRequest, ChatResponse, SessionHistoryResponse
from app.services.agent_engine_client import run_chat

router = APIRouter(prefix="/agent", tags=["agent"])

# In-memory session history for local/dev (replace with Agent Engine session store in prod)
_SESSION_HISTORY: Dict[str, list] = {}


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    user: Dict[str, Any] = Depends(get_current_user),
) -> ChatResponse:
    slug = require_constituency(body.constituency_id, user)
    result = run_chat(
        question=body.question,
        constituency_id=slug,
        session_id=body.session_id,
        user=user,
    )
    sid = result.get("session_id", "")
    _SESSION_HISTORY.setdefault(sid, []).append(
        {"role": "user", "content": body.question}
    )
    _SESSION_HISTORY[sid].append(
        {"role": "assistant", "content": result.get("answer", "")}
    )
    return ChatResponse(
        answer=result.get("answer", ""),
        charts=result.get("charts") or [],
        session_id=sid,
        agent_path=result.get("agent_path") or [],
        latency_ms=result.get("latency_ms"),
    )


@router.get("/sessions/{session_id}/history", response_model=SessionHistoryResponse)
def session_history(
    session_id: str,
    user: Dict[str, Any] = Depends(get_current_user),
) -> SessionHistoryResponse:
    turns = _SESSION_HISTORY.get(session_id, [])
    return SessionHistoryResponse(session_id=session_id, turns=turns)
