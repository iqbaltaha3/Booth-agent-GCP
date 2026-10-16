"""
Main chat endpoint — the only entry the frontend uses for Q&A.
"""

from __future__ import annotations

from typing import Any
from threading import RLock
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user, require_constituency
from app.models import ChatRequest, ChatResponse, SessionHistoryResponse
from app.services.agent import run_chat

router = APIRouter(prefix="/agent", tags=["agent"])

# In-memory session history for local/dev (replace with Agent Engine session store in prod)
_SESSION_HISTORY: dict[tuple, list] = {}
_SESSION_SEATS: dict[tuple, str] = {}
_SESSION_PENDING: dict[tuple, dict] = {}
# Serialize a session's turns so a clarification reply cannot race its parent.
_SESSION_LOCKS: dict[tuple, RLock] = {}
_LOCKS_GUARD = RLock()


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> ChatResponse:
    """Check seat access, ask Arjun, and save this user's conversation."""
    slug = require_constituency(body.constituency_id, user)
    sid = body.session_id or str(uuid4())
    key = (user["uid"], sid)
    with _LOCKS_GUARD:
        lock = _SESSION_LOCKS.setdefault(key, RLock())
    with lock:
        return _chat_turn(body, slug, key, sid)


def _chat_turn(body: ChatRequest, slug: str, key: tuple, sid: str) -> ChatResponse:
    """Execute one turn with conversation state scoped to its authorized user."""
    if key in _SESSION_SEATS and _SESSION_SEATS[key] != slug:
        raise HTTPException(403, "Session belongs to another constituency")
    try:
        result = run_chat(
            question=body.question,
            constituency_id=slug,
            session_id=sid,
            history=list(_SESSION_HISTORY.get(key, [])),
            pending_state=_SESSION_PENDING.get(key),
        )
    except RuntimeError as error:
        raise HTTPException(502, "AI service unavailable") from error
    # The backend owns the ID and state; a runtime cannot switch conversations.
    pending = result.get("pending_state")
    if pending:
        _SESSION_PENDING[key] = pending
    else:
        _SESSION_PENDING.pop(key, None)
    _SESSION_SEATS[key] = slug
    _SESSION_HISTORY.setdefault(key, []).append({"role": "user", "content": body.question})
    _SESSION_HISTORY[key].append({"role": "assistant", "content": result.get("answer", "")})
    return ChatResponse(
        answer=result.get("answer", ""),
        charts=result.get("charts") or [],
        session_id=sid,
        agent_path=result.get("agent_path") or [],
        latency_ms=result.get("latency_ms"),
        status=result.get("status", "complete"),
    )


@router.get("/sessions/{session_id}/history", response_model=SessionHistoryResponse)
def session_history(
    session_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> SessionHistoryResponse:
    """Return only the signed-in user's conversation history."""
    key = (user["uid"], session_id)
    if key in _SESSION_SEATS:
        require_constituency(_SESSION_SEATS[key], user)
    turns = _SESSION_HISTORY.get(key, [])
    return SessionHistoryResponse(session_id=session_id, turns=list(turns))
