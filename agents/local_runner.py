"""
Local in-process agent runner.

Ports the original ReAct booth / voter / history / portfolio agents so the
backend can answer questions without Vertex AI Agent Engine during
development.  In production the same logic runs inside Agent Engine and
calls the backend HTTP tools.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
AGENTS_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = AGENTS_DIR / "prompts"
REPO_ROOT = AGENTS_DIR.parent

# Ensure backend package is importable when running from repo root
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services import gcs_data  # noqa: E402
from app.services.sql_safety import validate_select  # noqa: E402


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
        "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    return payload


def gemini_chat(messages: List[Dict[str, str]], temperature: float = 0.1, max_tokens: int = 4096) -> str:
    model = DEFAULT_MODEL
    if model.startswith("models/"):
        model = model[len("models/") :]
    if not GEMINI_API_KEY:
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
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            candidates = parsed.get("candidates") or []
            if not candidates:
                return "<<LLM_ERROR: empty candidates>>"
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text = "".join(str(p.get("text", "")) for p in parts if not p.get("thought")).strip()
            return text or "<<LLM_ERROR: empty response>>"
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
                time.sleep(min(30, 2 ** attempt))
                continue
            return f"<<LLM_ERROR: HTTP {e.code}>>"
        except Exception as e:
            return f"<<LLM_ERROR: {e}>>"
    return "<<LLM_ERROR: retries exhausted>>"


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


def extract_sql(text: str) -> Optional[str]:
    if not text:
        return None
    m = re.search(r"```sql\s*(.*?)```", text, flags=re.I | re.S)
    if m:
        ok, cleaned = validate_select(m.group(1))
        if ok:
            return cleaned
    m = re.search(r"(?is)\b(select|with)\b", text)
    if m:
        ok, cleaned = validate_select(text[m.start() :])
        if ok:
            return cleaned
    return None


def load_prompt(filename: str, **kwargs: str) -> str:
    path = PROMPTS_DIR / filename
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        return text.format(**kwargs)
    except KeyError:
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


# ---------------------------------------------------------------------------
# ReAct state
# ---------------------------------------------------------------------------

@dataclass
class ReactStep:
    step: int
    thought: str
    action: str
    action_input: str
    observation: str


@dataclass
class ReactState:
    question: str
    steps: List[ReactStep] = field(default_factory=list)

    def memory(self, max_chars: int = 14000) -> str:
        parts = [
            f"Step {s.step}\nThought: {s.thought}\nAction: {s.action}\n"
            f"Input: {s.action_input}\nObservation: {s.observation[:4000]}"
            for s in self.steps
        ]
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            return "[older steps truncated]\n" + text[-max_chars:]
        return text


# ---------------------------------------------------------------------------
# Sub-agents
# ---------------------------------------------------------------------------

def run_voter_agent(question: str, slug: str, meta: Dict[str, str]) -> str:
    system = load_prompt("voter_agent_system.txt", **meta)
    # Live schema context
    try:
        conn = gcs_data.open_sqlite(slug, "voters.db")
        tables = gcs_data.list_tables(conn)
        lines = [f"DATABASE: voters.db"]
        samples = {}
        for t in tables:
            cols = gcs_data.list_columns(conn, t)
            lines.append(f"TABLE {t}: {', '.join(cols)}")
            if t == "voters":
                for col in ("region", "caste", "religion", "gender", "social_category"):
                    if col in cols:
                        samples[col] = gcs_data.sample_values(conn, t, col, limit=30)
        live = "\n".join(lines)
        if samples:
            live += "\n" + "\n".join(f"Sample DISTINCT {k}: {v}" for k, v in samples.items())
        conn.close()
    except Exception as e:
        live = f"(schema unavailable: {e})"

    system = system + "\n\nLIVE DATABASE CONTEXT\n---------------------\n" + wrap_electoral(live)

    state = ReactState(question=question)
    for turn in range(1, 6):
        user = f"User question:\n{question}\n\nResearch so far:\n{state.memory() or '(none yet)'}\n\nReturn the next JSON plan only."
        text = gemini_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1,
        )
        if text.startswith("<<LLM_ERROR"):
            return text
        parsed = safe_json(text) or {}
        action = str(parsed.get("action", "")).lower().strip()
        if action == "finish":
            break
        sql = str(parsed.get("sql", "")).strip() or (extract_sql(text) or "")
        ok, cleaned = validate_select(sql) if sql else (False, "")
        if not ok:
            state.steps.append(
                ReactStep(turn, str(parsed.get("thought", "")), "sql", sql, f"ERROR: {cleaned}")
            )
            continue
        try:
            conn = gcs_data.open_sqlite(slug, "voters.db")
            result = gcs_data.execute_select(conn, cleaned)
            conn.close()
            obs = json.dumps(result, ensure_ascii=False, default=str)[:4000]
        except Exception as e:
            obs = f"ERROR: {e}"
        state.steps.append(ReactStep(turn, str(parsed.get("thought", "")), "sql", cleaned, obs))

    # Synthesize
    good = [s for s in state.steps if s.action == "sql" and not s.observation.startswith("ERROR")]
    if not good:
        return "I could not produce a reliable answer from the voter roll for this question."
    blocks = "\n\n".join(f"SQL:\n{s.action_input}\nResult:\n{s.observation[:3000]}" for s in good)
    return gemini_chat(
        [
            {
                "role": "system",
                "content": "You are the Voter Agent. Answer using ONLY the SQL evidence. Do not invent numbers. Be clear and concise.",
            },
            {"role": "user", "content": f"Question: {question}\n\nEvidence:\n{blocks}\n\nWrite the final answer."},
        ],
        temperature=0.1,
    )


def run_history_agent(question: str, slug: str, meta: Dict[str, str]) -> str:
    system = load_prompt("history_agent_system.txt", **meta)
    try:
        conn = gcs_data.open_sqlite(slug, "history.db")
        tables = gcs_data.list_tables(conn)
        lines = [f"DATABASE: history.db"]
        for t in tables:
            cols = gcs_data.list_columns(conn, t)
            lines.append(f"TABLE {t}: {', '.join(cols)}")
            if "party" in cols:
                lines.append(f"  DISTINCT party: {gcs_data.sample_values(conn, t, 'party', 25)}")
            if "area" in cols:
                lines.append(f"  Sample area: {gcs_data.sample_values(conn, t, 'area', 25)}")
        live = "\n".join(lines)
        conn.close()
    except Exception as e:
        live = f"(schema unavailable: {e})"

    system = system + "\n\nLIVE DATABASE CONTEXT\n---------------------\n" + wrap_electoral(live)

    state = ReactState(question=question)
    for turn in range(1, 7):
        user = f"User question:\n{question}\n\nResearch so far:\n{state.memory() or '(none yet)'}\n\nReturn the next JSON plan only."
        text = gemini_chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1,
        )
        if text.startswith("<<LLM_ERROR"):
            return text
        parsed = safe_json(text) or {}
        action = str(parsed.get("action", "")).lower().strip()
        if action == "finish":
            break
        sql = str(parsed.get("sql", "")).strip() or (extract_sql(text) or "")
        ok, cleaned = validate_select(sql) if sql else (False, "")
        if not ok:
            state.steps.append(
                ReactStep(turn, str(parsed.get("thought", "")), "sql", sql, f"ERROR: {cleaned}")
            )
            continue
        try:
            conn = gcs_data.open_sqlite(slug, "history.db")
            result = gcs_data.execute_select(conn, cleaned)
            conn.close()
            obs = json.dumps(result, ensure_ascii=False, default=str)[:4000]
        except Exception as e:
            obs = f"ERROR: {e}"
        state.steps.append(ReactStep(turn, str(parsed.get("thought", "")), "sql", cleaned, obs))

    good = [s for s in state.steps if s.action == "sql" and not s.observation.startswith("ERROR")]
    if not good:
        return "I could not produce a reliable answer from the election history for this question."
    blocks = "\n\n".join(f"SQL:\n{s.action_input}\nResult:\n{s.observation[:3500]}" for s in good)
    return gemini_chat(
        [
            {
                "role": "system",
                "content": "You are the History Agent. Answer using ONLY the SQL evidence. Do not invent numbers.",
            },
            {"role": "user", "content": f"Question: {question}\n\nEvidence:\n{blocks}\n\nWrite the final answer."},
        ],
        temperature=0.1,
    )


def run_portfolio_agent(question: str, slug: str, meta: Dict[str, str]) -> str:
    system = load_prompt("portfolio_agent_system.txt", **meta)
    # Extract targets
    extract_prompt = """
You extract booth targets from a user question.
Return ONLY JSON:
{"part_numbers": ["Part_1"], "booth_names": ["PRIMARY SCHOOL ..."]}
Normalize part mentions to Part_N. Empty lists if nothing mentioned.
""".strip()
    text = gemini_chat(
        [{"role": "system", "content": extract_prompt}, {"role": "user", "content": f"User query: {question}"}],
        temperature=0.0,
    )
    parsed = safe_json(text) or {}
    parts = parsed.get("part_numbers") or []
    names = parsed.get("booth_names") or []

    # Load boothlist + search
    evidence = []
    try:
        rows = gcs_data.load_boothlist(slug)
        files = gcs_data.list_portfolio_files(slug)
        import difflib
        stations = []
        for r in rows:
            name = (r.get("pooling_station") or r.get("polling_station") or r.get("booth_name") or "").strip()
            part = str(r.get("part_no") or r.get("part_number") or "").strip()
            if name:
                stations.append((name, part))

        matched_files = []
        for p in parts:
            m = re.search(r"(\d+)", str(p))
            if m:
                target = f"Part_{int(m.group(1))}"
                for name, part in stations:
                    if part.lower() == target.lower() or part.lower() == str(int(m.group(1))):
                        # find json
                        for f in files:
                            if name.lower().replace(" ", "_") in f.lower() or target.lower() in f.lower():
                                matched_files.append(f)
                                break
        for n in names:
            close = difflib.get_close_matches(n, [s[0] for s in stations], n=2, cutoff=0.4)
            for c in close:
                for f in files:
                    if c.lower().replace(" ", "_") in f.lower():
                        matched_files.append(f)

        matched_files = list(dict.fromkeys(matched_files))[:3]
        for f in matched_files:
            data = gcs_data.load_portfolio_json(slug, f)
            evidence.append(f"FILE: {f}\n{json.dumps(data, ensure_ascii=False, default=str)[:3500]}")
    except Exception as e:
        evidence.append(f"ERROR loading portfolios: {e}")

    if not evidence:
        return "I could not find a matching booth portfolio for that question."

    return gemini_chat(
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"Question: {question}\n\nEvidence:\n"
                + wrap_electoral("\n\n".join(evidence))
                + "\n\nWrite a clear final answer from the portfolio data only.",
            },
        ],
        temperature=0.1,
    )


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

def run_local_agent(
    question: str,
    constituency_id: str,
    session_id: str = "",
) -> Dict[str, Any]:
    slug = constituency_id.strip().lower()
    meta = load_constituency_meta(slug)
    system = load_prompt("coordinator_system.txt", **meta)

    # Simple routing: ask the coordinator which specialist(s) to call
    route_user = f"""
User question: {question}

Decide which specialist(s) to call. Return ONLY JSON:
{{
  "thought": "...",
  "tools": ["voter"] | ["history"] | ["portfolio"] | ["voter","history"] | ...
}}
Valid tools: voter, history, portfolio.
If the question is pure greeting / capabilities, tools can be [].
"""
    text = gemini_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": route_user}],
        temperature=0.1,
    )
    parsed = safe_json(text) or {}
    tools = [str(t).lower().strip() for t in (parsed.get("tools") or [])]

    if not tools:
        # Capabilities / greeting
        answer = (
            f"I am {meta.get('product_name', 'Arjun')} for "
            f"{meta.get('name', slug)} ({meta.get('district', '')}, {meta.get('state', '')}). "
            "Ask me about the voter roll, election history (Form-20), or booth portfolios."
        )
        return {"answer": answer, "charts": [], "session_id": session_id, "agent_path": []}

    results = []
    agent_path = []
    if "voter" in tools:
        agent_path.append("voter_agent")
        results.append(("Voter Agent", run_voter_agent(question, slug, meta)))
    if "history" in tools:
        agent_path.append("history_agent")
        results.append(("History Agent", run_history_agent(question, slug, meta)))
    if "portfolio" in tools:
        agent_path.append("portfolio_agent")
        results.append(("Portfolio Agent", run_portfolio_agent(question, slug, meta)))

    if len(results) == 1:
        answer = results[0][1]
    else:
        evidence = "\n\n".join(f"### {name}\n{ans}" for name, ans in results)
        answer = gemini_chat(
            [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": f"Question: {question}\n\nSpecialist findings:\n{evidence}\n\n"
                    "Synthesise one clear final answer for the campaign user. "
                    "Do not invent numbers.",
                },
            ],
            temperature=0.1,
        )

    return {
        "answer": answer,
        "charts": [],
        "session_id": session_id,
        "agent_path": agent_path,
    }
