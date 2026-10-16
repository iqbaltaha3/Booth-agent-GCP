"""Send a question to the local runner or Google's deployed Agent Engine."""

import logging
import time
import uuid

from app.config import get_settings


def run_chat(
    question: str, constituency_id: str, session_id: str | None = None,
    history: list[dict] | None = None, pending_state: dict | None = None,
) -> dict:
    """Choose the configured AI runtime and measure its response time."""
    settings = get_settings()
    session_id = session_id or str(uuid.uuid4())
    started = time.perf_counter()
    if settings.agent_local_mode or not settings.agent_engine_resource_name:
        # Import only when needed so cloud mode does not initialize the local runner.
        from agents.local_runner import run_local_agent

        result = run_local_agent(
            question=question, constituency_id=constituency_id, session_id=session_id,
            history=history, pending_state=pending_state,
        )
    else:
        result = _call_agent_engine(question, constituency_id, session_id, history, pending_state)
    result.setdefault("session_id", session_id)
    result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    return result


def _call_agent_engine(
    question: str, constituency_id: str, session_id: str,
    history: list[dict] | None = None, pending_state: dict | None = None,
) -> dict:
    """Use Google's SDK and normalize the deployed engine's response."""
    import vertexai
    from vertexai import agent_engines

    settings = get_settings()
    try:
        vertexai.init(location=settings.region)
        engine = agent_engines.get(settings.agent_engine_resource_name)
        response = engine.query(
            input={"question": question, "constituency_id": constituency_id, "session_id": session_id,
                   "history": history or [], "pending_state": pending_state}
        )
    except Exception as error:
        logging.exception("Agent Engine call failed")
        raise RuntimeError("Agent Engine is unavailable") from error
    if not isinstance(response, dict):
        response = {"answer": str(response)}
    return {
        "answer": response.get("answer") or response.get("output") or str(response),
        "charts": response.get("charts") or [],
        "session_id": session_id,
        "agent_path": response.get("agent_path") or [],
        "status": response.get("status", "complete"),
        "pending_state": response.get("pending_state"),
    }
