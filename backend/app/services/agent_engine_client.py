"""
Thin wrapper around Vertex AI Agent Engine (or local in-process agents).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


def run_chat(
    question: str,
    constituency_id: str,
    session_id: Optional[str] = None,
    user: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Returns {answer, charts, session_id, agent_path, latency_ms}.
    """
    settings = get_settings()
    session_id = session_id or str(uuid.uuid4())
    t0 = time.time()

    if settings.agent_local_mode or not settings.agent_engine_resource_name:
        # In-process local runner (same agent logic, no Vertex required)
        from agents.local_runner import run_local_agent

        result = run_local_agent(
            question=question,
            constituency_id=constituency_id,
            session_id=session_id,
        )
    else:
        result = _call_agent_engine(
            question=question,
            constituency_id=constituency_id,
            session_id=session_id,
            resource_name=settings.agent_engine_resource_name,
        )

    latency = int((time.time() - t0) * 1000)
    result.setdefault("session_id", session_id)
    result["latency_ms"] = latency
    return result


def _call_agent_engine(
    question: str,
    constituency_id: str,
    session_id: str,
    resource_name: str,
) -> Dict[str, Any]:
    """
    Call the deployed Vertex AI Agent Engine reasoning engine.
    """
    try:
        import vertexai
        from vertexai import agent_engines

        settings = get_settings()
        vertexai.init(project=None, location=settings.region)  # ADC
        engine = agent_engines.get(resource_name)
        response = engine.query(
            input={
                "question": question,
                "constituency_id": constituency_id,
                "session_id": session_id,
            }
        )
        # Normalise response shape
        if isinstance(response, dict):
            return {
                "answer": response.get("answer") or response.get("output") or str(response),
                "charts": response.get("charts") or [],
                "session_id": session_id,
                "agent_path": response.get("agent_path") or [],
            }
        return {
            "answer": str(response),
            "charts": [],
            "session_id": session_id,
            "agent_path": [],
        }
    except Exception as e:
        logger.exception("Agent Engine call failed")
        return {
            "answer": f"Agent Engine error: {e}",
            "charts": [],
            "session_id": session_id,
            "agent_path": [],
        }
