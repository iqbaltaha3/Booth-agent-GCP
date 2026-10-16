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


# ---------------------------------------------------------------------------
# Gemini client (same as original common.py)
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = (
    os.environ.get("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/models")
).rstrip("/")
TIMEOUT = int(os.environ.get("GEMINI_TIMEOUT", "120"))
MAX_ATTEMPTS = int(os.environ.get("GEMINI_MAX_ATTEMPTS", "3"))


def _to_gemini_payload(messages: List[Dict[str, str]], temperature: float, max_tokens: int) -> Dict:
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
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    return payload


def gemini_chat(messages: List[Dict[str, str]], temperature: float = 0.1, max_tokens: int = 4096, deadline: float | None = None) -> str:
    model = DEFAULT_MODEL
    if model.startswith("models/"):
        model = model[len("models/") :]
    if not GEMINI_API_KEY:
        if deadline is not None:
            return vertex_deadline_chat(messages, model, temperature, max_tokens, deadline)
        return vertex_gemini_chat(messages, model, temperature, max_tokens)
    data = json.dumps(_to_gemini_payload(messages, temperature, max_tokens)).encode("utf-8")
    req = urllib.request.Request(
        f"{GEMINI_URL}/{model}:generateContent",
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
            "User-Agent": "ArjunBoothAgent/2.0",
        },
        method="POST",
    )
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            remaining = TIMEOUT if deadline is None else min(TIMEOUT, deadline - time.monotonic())
            if remaining <= 0:
                return "<<LLM_ERROR: deadline exceeded>>"
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            candidates = parsed.get("candidates") or []
            if not candidates:
                return "<<LLM_ERROR: empty candidates>>"
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(str(p.get("text", "")) for p in parts if not p.get("thought")).strip()
            return text or "<<LLM_ERROR: empty response>>"
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                delay = min(30, 2 ** attempt)
                if deadline is not None and time.monotonic() + delay >= deadline:
                    return "<<LLM_ERROR: deadline exceeded>>"
                time.sleep(delay)
                continue
            return f"<<LLM_ERROR: HTTP {e.code}>>"
        except Exception as e:
            return f"<<LLM_ERROR: {e}>>"
    return "<<LLM_ERROR: retries exhausted>>"


def vertex_deadline_chat(messages, model, temperature, max_tokens, deadline):
    """Use the same Vertex model/ADC through REST with a per-research deadline."""
    try:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        credentials, detected_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT") or detected_project
        location = os.environ.get("REGION") or os.environ.get("GCP_REGION") or "asia-south1"
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not project:
            return "<<LLM_ERROR: deadline or project unavailable>>"
        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        url = f"https://{host}/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent"
        with AuthorizedSession(credentials) as session:
            response = session.post(url, json=_to_gemini_payload(messages, temperature, max_tokens),
                                    timeout=min(TIMEOUT, remaining), max_allowed_time=remaining)
            if not response.ok:
                return f"<<LLM_ERROR: Vertex HTTP {response.status_code}>>"
            parts = ((response.json().get("candidates") or [{}])[0].get("content") or {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip() or "<<LLM_ERROR: empty response>>"
    except Exception:
        return "<<LLM_ERROR: Vertex research request failed>>"


def vertex_gemini_chat(
    messages: List[Dict[str, str]],
    model: str,
    temperature: float,
    max_tokens: int,
) -> str:
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel, GenerationConfig

        project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCP_PROJECT")
        location = os.environ.get("REGION") or os.environ.get("GCP_REGION") or "asia-south1"
        vertexai.init(project=project, location=location)

        system_parts = []
        prompt_parts = []
        for message in messages:
            role = message.get("role", "user")
            text = str(message.get("content", ""))
            if role == "system":
                system_parts.append(text)
            else:
                prompt_parts.append(f"{role.upper()}:\n{text}")

        client = GenerativeModel(
            model,
            system_instruction="\n\n".join(system_parts) if system_parts else None,
        )
        response = client.generate_content(
            "\n\n".join(prompt_parts),
            generation_config=GenerationConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
            ),
        )
        return (response.text or "").strip() or "<<LLM_ERROR: empty response>>"
    except Exception as e:
        return f"<<LLM_ERROR: Vertex Gemini: {e}>>"


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

    return run_coordinator(
        question, constituency_id, session_id, history, pending_state,
        runner=sys.modules[__name__],
        voter_context=voter_context,
    )
