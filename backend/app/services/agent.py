"""Invoke the LangGraph agent harness and measure its response time."""

import time
import uuid

from agents.local_runner import run_local_agent


def run_chat(
    question: str,
    constituency_id: str,
    session_id: str | None = None,
    history: list[dict] | None = None,
    pending_state: dict | None = None,
    voter_context: dict | None = None,
) -> dict:
    session_id = session_id or str(uuid.uuid4())
    started = time.perf_counter()
    result = run_local_agent(
        question=question,
        constituency_id=constituency_id,
        session_id=session_id,
        history=history,
        pending_state=pending_state,
        voter_context=voter_context,
    )
    result.setdefault("session_id", session_id)
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return result
