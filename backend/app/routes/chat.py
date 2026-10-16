"""
Main chat endpoint — the only entry the frontend uses for Q&A.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user, require_constituency
from app.models import ChatRequest, ChatResponse, SessionHistoryResponse
from app.services.agent import run_chat
from app.services.sessions import get_store, SessionSeatMismatch, SessionBusy

router = APIRouter(prefix="/agent", tags=["agent"])

@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> ChatResponse:
    """Check seat access, ask Arjun, and save this user's conversation."""
    slug = require_constituency(body.constituency_id, user)
    sid = body.session_id or str(uuid4())
    try:
        with get_store().turn(user["uid"], sid, slug) as state:
            try:
                result = run_chat(
                    question=body.question, constituency_id=slug, session_id=sid,
                    history=list(state["history"]), pending_state=state["pending_state"],
                    voter_context=state.get("voter_context", {}),
                )
            except RuntimeError as error:
                raise HTTPException(502, "AI service unavailable") from error
            state["pending_state"] = result.get("pending_state")
            state["voter_context"] = result.get("voter_context", state.get("voter_context", {}))
            state["diagnostics"] = (state.get("diagnostics", []) + result.get("diagnostics", []))[-16:]
            response = ChatResponse(
                answer=result.get("answer", ""), charts=result.get("charts") or [],
                session_id=sid, agent_path=result.get("agent_path") or [],
                latency_ms=result.get("latency_ms"), status=result.get("status", "complete"),
                sources=result.get("sources", []),
            )
            state["history"] = (state["history"] + [
                {"role": "user", "content": body.question},
                {"role": "assistant", "content": response.answer,
                 "sources": response.model_dump(mode="json")["sources"]},
            ])[-24:]
        return response
    except SessionSeatMismatch as error:
        raise HTTPException(403, str(error)) from error
    except SessionBusy as error:
        raise HTTPException(409, str(error)) from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(503, "Conversation storage unavailable; please retry") from error


@router.get("/sessions/{session_id}/history", response_model=SessionHistoryResponse)
def session_history(
    session_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> SessionHistoryResponse:
    """Return only the signed-in user's conversation history."""
    try:
        state = get_store().read(user["uid"], session_id)
    except Exception as error:
        raise HTTPException(503, "Conversation storage unavailable") from error
    if state:
        require_constituency(state["constituency_id"], user)
    return SessionHistoryResponse(session_id=session_id, turns=state["history"] if state else [])
