"""
Local in-process agent runner.

Gemini transport and shared helpers for the coordinator and specialist runtime.
The backend uses this runner in local mode or when no Agent Engine is configured.
Agent Engine deployment is a separate, currently unfinished integration.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import random
from threading import BoundedSemaphore
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
AGENTS_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = AGENTS_DIR / "prompts"
REPO_ROOT = AGENTS_DIR.parent

# Ensure backend package is importable when running from repo root
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services import gcs_data  # noqa: E402
from agents import execution
from agents.provider_errors import ProviderReply, retry_after_seconds


# ---------------------------------------------------------------------------
# Gemini client (same as original common.py)
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    os.environ.get("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/models")
).rstrip("/")
TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "120"))
MAX_ATTEMPTS = max(1, min(int(os.environ.get("GEMINI_MAX_ATTEMPTS", "3")), 5))
_MODEL_SLOTS = BoundedSemaphore(max(1, int(os.environ.get("GEMINI_MAX_CONCURRENT_CALLS", "4"))))


def _to_gemini_payload(messages: List[Dict[str, str]], temperature: float, max_tokens: int, response_schema=None) -> Dict:
    system_parts, contents = [], []
    for m in messages:
        role, text = m.get("role", "user"), str(m.get("content", ""))
        if role == "system":
            system_parts.append(text)
            continue
        g_role = "model" if role == "assistant" else "user"
        if contents and contents[-1]["role"] == g_role:
            contents[-1]["parts"][0]["text"] += "\n\n" + text
        else:
            contents.append({"role": g_role, "parts": [{"text": text}]})
    payload: Dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature, "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
        },
    }
    if response_schema is not None:
        payload["generationConfig"]["responseSchema"] = execution.provider_schema(response_schema)
    if DEFAULT_MODEL.removeprefix("models/").startswith("gemini-2.5-flash"):
        # Reserve output capacity for actions/findings instead of unbounded
        # hidden thinking. Short transliteration calls need no thinking budget.
        budget = max(0, min(int(os.environ.get("GEMINI_THINKING_BUDGET", "1024")), 24576))
        payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": min(budget, max_tokens // 3) if max_tokens >= 1024 else 0}
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    return payload


def _response_text(parsed):
    candidates = parsed.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    reason = candidate.get("finishReason", "")
    usage = parsed.get("usageMetadata", {})
    execution.record({"event": "model_response", "finish_reason": reason,
                      "tokens": {k: usage[k] for k in ("promptTokenCount", "candidatesTokenCount", "thoughtsTokenCount") if k in usage}})
    if reason == "MAX_TOKENS":
        return "<<LLM_ERROR: output_truncated>>"
    if reason and reason != "STOP":
        return "<<LLM_ERROR: generation_blocked>>"
    parts = (candidate.get("content") or {}).get("parts") or []
    return "".join(str(p.get("text", "")) for p in parts if not p.get("thought")).strip() or "<<LLM_ERROR: empty_response>>"


def _gemini_chat(messages: List[Dict[str, str]], temperature: float = 0.1, max_tokens: int = 4096,
                deadline: float | None = None, response_schema=None) -> str:
    """One bounded transport for all agent calls; no private response text is logged."""
    end = execution.deadline(deadline) or time.monotonic() + TIMEOUT
    model = DEFAULT_MODEL.removeprefix("models/")
    started = time.monotonic()
    for attempt in range(MAX_ATTEMPTS):
        if time.monotonic() >= end:
            return "<<LLM_ERROR: deadline_exceeded>>"
        if GEMINI_API_KEY:
            data = json.dumps(_to_gemini_payload(messages, temperature, max_tokens, response_schema)).encode("utf-8")
            req = urllib.request.Request(f"{GEMINI_URL}/{model}:generateContent", data=data,
                  headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=min(TIMEOUT, end - time.monotonic())) as response:
                    result = _response_text(json.loads(response.read().decode("utf-8")))
            except urllib.error.HTTPError as error:
                result = ProviderReply(f"<<LLM_ERROR: HTTP {error.code}>>", error.code, retry_after_seconds(error.headers.get("Retry-After")))
            except Exception:
                result = "<<LLM_ERROR: provider_unavailable>>"
        else:
            result = vertex_deadline_chat(messages, model, temperature, max_tokens, end, response_schema)
        if attempt == 0 and "output_truncated" in result:
            max_tokens = min(max_tokens * 2, 8192)
            messages = messages + [{"role": "user", "content":
                "The response exceeded its output budget. Return a concise structured response. "
                "For delegation, use at most two tasks, short objectives, and at most five outputs/criteria. "
                "Omit unused optional fields and do not enumerate data records in an assignment. "
                "For findings, return at most six concise supported findings."}]
            continue
        if any(f"HTTP {code}" in result for code in (429, 500, 502, 503, 504)):
            execution.record({"event": "provider_attempt", "attempt": attempt + 1, "model": model,
                              "endpoint": os.environ.get("VERTEX_LOCATION") or os.environ.get("REGION", "asia-south1"),
                              "status_code": int(re.search(r"HTTP (\d{3})", result)[1])})
            delay = max(min(8, 2 ** attempt) + random.uniform(0, 1), getattr(result, "retry_after", None) or 0)
            if attempt + 1 < MAX_ATTEMPTS and time.monotonic() + delay + 1 < end:
                time.sleep(delay)
                continue
        execution.record({"event": "model_call", "elapsed_ms": int((time.monotonic() - started) * 1000),
                          "schema": (response_schema or {}).get("title"),
                          "error_kind": result if result.startswith("<<LLM_ERROR") else None,
                          "outcome": "failed" if result.startswith("<<LLM_ERROR") else "ok"})
        return result
    return result


def gemini_chat(messages, temperature=0.1, max_tokens=4096, deadline=None, response_schema=None):
    # Admission control is per process; Cloud Run instance/concurrency limits
    # still bound service-wide demand. Waiting consumes the same turn deadline.
    remaining = execution.remaining(reserve=15)
    if remaining <= 0:
        return "<<LLM_ERROR: deadline_exceeded>>"
    if not _MODEL_SLOTS.acquire(timeout=min(10, remaining)):
        execution.record({"event": "provider_admission", "status_code": 429})
        return "<<LLM_ERROR: HTTP 429 local capacity exhausted>>"
    try:
        return _gemini_chat(messages, temperature, max_tokens, deadline, response_schema)
    finally:
        _MODEL_SLOTS.release()


def vertex_deadline_chat(messages, model, temperature, max_tokens, deadline, response_schema=None):
    """Use the same Vertex model/ADC through REST with a per-research deadline."""
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, detected_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT") or detected_project
        location = os.environ.get("VERTEX_LOCATION") or os.environ.get("REGION") or os.environ.get("GCP_REGION") or "asia-south1"
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not project:
            return "<<LLM_ERROR: deadline or project unavailable>>"
        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        url = f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent"
        with AuthorizedSession(credentials) as session:
            response = session.post(url, json=_to_gemini_payload(messages, temperature, max_tokens, response_schema),
                                    timeout=min(TIMEOUT, remaining), max_allowed_time=remaining)
            if not response.ok:
                return ProviderReply(f"<<LLM_ERROR: Vertex HTTP {response.status_code}>>", response.status_code, retry_after_seconds(response.headers.get("Retry-After")))
            return _response_text(response.json())
    except Exception:
        return "<<LLM_ERROR: provider_unavailable>>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    text = text.strip()
    for candidate in (text, re.sub(r"```(?:json)?", "", text, flags=re.I).replace("```", "").strip()):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def load_prompt(filename: str, **kwargs: str) -> str:
    path = PROMPTS_DIR / filename
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    # Replace only known template names; JSON examples may contain literal braces.
    for key, value in kwargs.items():
        text = text.replace("{" + key + "}", str(value))
    examples_path = PROMPTS_DIR / "few_shot_examples.json"
    if examples_path.exists():
        examples = json.loads(examples_path.read_text(encoding="utf-8")).get(filename, [])
        if examples:
            text += "\nFEW-SHOT EXAMPLES (fictional records, not current facts):\n" + json.dumps(examples, ensure_ascii=False)
    return text


def load_constituency_meta(slug: str) -> Dict[str, str]:
    yaml_path = REPO_ROOT / "constituencies" / slug / "constituency.yaml"
    meta = {
        "slug": slug,
        "name": slug.title(),
        "seat_type": "Vidhan Sabha (Assembly)",
        "district": "",
        "state": "",
        "product_name": "Arjun",
        "product_tagline": "",
    }
    if yaml_path.exists():
        try:
            import yaml
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            meta.update({k: str(v) for k, v in data.items() if v is not None})
        except Exception:
            pass
    from app.services.data import load_coverage
    meta["data_coverage"] = json.dumps(load_coverage(slug), ensure_ascii=False)
    return meta


def wrap_electoral(text: str) -> str:
    return f"<electoral_data>\n{text}\n</electoral_data>"


def wants_hindi(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u097F]", text or ""))


def answer_style_instruction(question: str) -> str:
    language = "Hindi" if wants_hindi(question) else "English"
    return f"""
Write the user-facing answer in {language}.

Presentation rules:
- Use plain text only. Do not use Markdown, asterisks, hashes, tables, or code blocks.
- Do not mention SQL, query, database, result rows, tool output, JSON, evidence, or internal agent names.
- Use short, simple, professional sentences.
- Start with a direct answer, then add useful context.
- For counts, show the number with commas and say what it represents.
- For simple total-count questions, use two short lines: the count, then what the count means.
- If helpful, use simple numbered lines like "1. Total voters: 111,537".
- Do not expose reasoning or internal process.
- Do not invent numbers.
""".strip()


def clean_user_answer(answer: str, question: str) -> str:
    text = str(answer or "").strip()
    if not text:
        return text

    replacements = {
        "**": "",
        "__": "",
        "```": "",
        "###": "",
        "##": "",
        "#": "",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    internal_patterns = [
        r"\bfrom the SQL results,?\s*",
        r"\bfrom SQL results,?\s*",
        r"\bbased on the SQL results,?\s*",
        r"\bbased on SQL results,?\s*",
        r"\baccording to the SQL query,?\s*",
        r"\bthe query shows that\s*",
        r"\bthe query returned\s*",
        r"\bSQL query\b",
        r"\bSQL\b",
        r"\bquery results\b",
        r"\bresult rows\b",
        r"\btool output\b",
        r"\bJSON\b",
        r"\bevidence\b",
        r"\bVoter Agent\b",
        r"\bHistory Agent\b",
        r"\bPortfolio Agent\b",
    ]
    for pattern in internal_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    text = re.sub(r":\s*,\s*", ": ", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*,", ",", text)

    lines = []
    for line in text.splitlines():
        cleaned = line.strip()
        cleaned = re.sub(r"^\s*[-*]\s+", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned)
        cleaned = cleaned.strip(" :")
        if cleaned:
            lines.append(cleaned)

    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text
def run_local_agent(
    question: str,
    constituency_id: str,
    session_id: str = "",
    history: list[dict] | None = None,
    pending_state: dict | None = None,
    voter_context: dict | None = None,
) -> Dict[str, Any]:
    """Run the coordinator with server-owned conversation and pending task state."""
    from agents.runtime import run_coordinator

    with execution.execution_scope(max(30, min(int(os.environ.get("AGENT_TIMEOUT_SECONDS", "240")), 540))):
        return run_coordinator(
            question, constituency_id, session_id, history, pending_state,
            runner=sys.modules[__name__], voter_context=voter_context,
        )
