"""LangChain Gemini boundary with bounded retries and private usage metadata."""

import os
import random
import time
from threading import BoundedSemaphore

from langchain_core.messages import convert_to_messages
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import tracing_context

from agents import execution
from agents.provider_errors import ProviderReply, retry_after_seconds

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "120"))
MAX_ATTEMPTS = max(1, min(int(os.environ.get("GEMINI_MAX_ATTEMPTS", "3")), 5))
_MODEL_SLOTS = BoundedSemaphore(
    max(1, int(os.environ.get("GEMINI_MAX_CONCURRENT_CALLS", "4")))
)


def _invoke_model(messages, temperature, max_tokens, deadline, response_schema):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "<<LLM_ERROR: deadline_exceeded>>"
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    vertex = (
        os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", str(not bool(key))).lower()
        == "true"
    )
    options = {
        "model": DEFAULT_MODEL.removeprefix("models/"),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": min(TIMEOUT, remaining),
        "max_retries": 0,
        "vertexai": vertex,
    }
    if key:
        options["api_key"] = key
    if vertex:
        options["project"] = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get(
            "GCP_PROJECT"
        )
        options["location"] = (
            os.environ.get("GOOGLE_CLOUD_LOCATION")
            or os.environ.get("VERTEX_LOCATION")
            or os.environ.get("REGION")
            or os.environ.get("GCP_REGION")
            or "asia-south1"
        )
    if options["model"].startswith("gemini-2.5-flash"):
        options["thinking_budget"] = (
            min(
                max(
                    0, min(int(os.environ.get("GEMINI_THINKING_BUDGET", "1024")), 24576)
                ),
                max_tokens // 3,
            )
            if max_tokens >= 1024
            else 0
        )
    try:
        model = ChatGoogleGenerativeAI(**options)
        kwargs = {"response_mime_type": "application/json"}
        if response_schema:
            kwargs["response_schema"] = execution.provider_schema(response_schema)
        with tracing_context(enabled=False):
            reply = model.bind(**kwargs).invoke(convert_to_messages(messages))
        finish = str(reply.response_metadata.get("finish_reason", "STOP")).upper()
        execution.record(
            {
                "event": "model_response",
                "finish_reason": finish,
                "tokens": reply.usage_metadata or {},
            }
        )
        if "MAX_TOKENS" in finish:
            return "<<LLM_ERROR: output_truncated>>"
        if finish not in {"STOP", "FINISH_REASON.STOP"}:
            return "<<LLM_ERROR: generation_blocked>>"
        if isinstance(reply.content, str):
            return reply.content.strip() or "<<LLM_ERROR: empty_response>>"
        return (
            "".join(
                block.get("text", "")
                for block in reply.content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
            or "<<LLM_ERROR: empty_response>>"
        )
    except Exception as error:
        # Never propagate provider bodies, input messages or credentials.
        current = error
        seen = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            code = getattr(current, "code", None) or getattr(
                current, "status_code", None
            )
            response = getattr(current, "response", None)
            code = (
                code
                if isinstance(code, int)
                else getattr(response, "status_code", None)
            )
            headers = getattr(response, "headers", {}) or {}
            if code:
                return ProviderReply(
                    f"<<LLM_ERROR: HTTP {code}>>",
                    code,
                    retry_after_seconds(headers.get("Retry-After")),
                )
            current = current.__cause__
        return "<<LLM_ERROR: provider_unavailable>>"


def chat(
    messages, temperature=0.1, max_tokens=4096, deadline=None, response_schema=None
):
    end = execution.deadline(deadline, reserve=15) or time.monotonic() + TIMEOUT
    remaining = end - time.monotonic()
    if remaining <= 0:
        return "<<LLM_ERROR: deadline_exceeded>>"
    if not _MODEL_SLOTS.acquire(timeout=min(10, remaining)):
        execution.record({"event": "provider_admission", "status_code": 429})
        return "<<LLM_ERROR: HTTP 429 local capacity exhausted>>"
    started = time.monotonic()
    result = "<<LLM_ERROR: deadline_exceeded>>"
    try:
        for attempt in range(MAX_ATTEMPTS):
            if time.monotonic() >= end:
                result = "<<LLM_ERROR: deadline_exceeded>>"
                break
            result = _invoke_model(
                messages, temperature, max_tokens, end, response_schema
            )
            if attempt == 0 and "output_truncated" in result:
                max_tokens = min(max_tokens * 2, 8192)
                messages = messages + [
                    {
                        "role": "user",
                        "content": "Return a concise structured response; omit unused fields and long record lists.",
                    }
                ]
                continue
            if getattr(result, "status_code", None) in {429, 500, 502, 503, 504}:
                execution.record(
                    {
                        "event": "provider_attempt",
                        "attempt": attempt + 1,
                        "model": DEFAULT_MODEL,
                        "status_code": result.status_code,
                    }
                )
                delay = max(
                    min(8, 2**attempt) + random.uniform(0, 1), result.retry_after or 0
                )
                if attempt + 1 < MAX_ATTEMPTS and time.monotonic() + delay + 1 < end:
                    time.sleep(delay)
                    continue
            break
        return result
    finally:
        execution.record(
            {
                "event": "model_call",
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "schema": (response_schema or {}).get("title"),
                "outcome": "failed" if result.startswith("<<LLM_ERROR") else "ok",
            }
        )
        _MODEL_SLOTS.release()
