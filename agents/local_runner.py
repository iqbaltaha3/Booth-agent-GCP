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
    total_voter_question = (
        re.search(r"\btotal\b", question or "", flags=re.IGNORECASE)
        and re.search(r"\bvoters?\b", question or "", flags=re.IGNORECASE)
    ) or bool(re.search(r"कुल.*मतदाता|मतदाता.*कुल", question or ""))
    if total_voter_question:
        match = re.search(r"([\d,]+)\s+(?:total\s+)?voters?", text, flags=re.IGNORECASE)
        if not match:
            match = re.search(r"\b([\d]{1,3}(?:,[\d]{3})+|[\d]+)\b", text)
        if match:
            count = match.group(1)
            if wants_hindi(question):
                return (
                    f"कुल मतदाता: {count}\n"
                    "यह चयनित विधानसभा क्षेत्र की मतदाता सूची में दर्ज कुल मतदाताओं की संख्या है।"
                )
            return (
                f"Total voters: {count}\n"
                "This is the total number of voters recorded for the selected constituency."
            )
    return text


RELATION_WORDS = "wife|husband|father|mother|son|daughter"
RELATION_TYPE_BY_WORD = {
    "father": "Father",
    "mother": "Mother",
    "husband": "Husband",
    "wife": "Husband",
    "son": "Father",
    "daughter": "Father",
}


def _roman_tokens(text: str) -> List[str]:
    return re.findall(r"[a-zA-Z]+", (text or "").lower())


def _split_devanagari_terms(text: str) -> List[str]:
    terms = []
    for term in re.findall(r"[\u0900-\u097F]+", text or ""):
        if len(term) > 1 and term not in terms:
            terms.append(term)
    return terms


def _transliterate_voter_phrases(person_phrase: str, relative_phrase: str) -> Tuple[List[str], List[str]]:
    prompt = """
Convert Romanized Hindi voter-roll names into Devanagari.

Return ONLY JSON:
{
  "person_terms": ["..."],
  "relative_terms": ["..."]
}

Rules:
- Convert each meaningful name word to likely Hindi Devanagari spellings.
- Include common alternate spellings when useful.
- Do not translate relationship words like father, mother, husband, wife.
- Do not include English words.
- Keep output short. Usually 1 to 4 terms per field.
- If a field is empty, return an empty list.
""".strip()
    user = json.dumps(
        {"person": person_phrase or "", "relative": relative_phrase or ""},
        ensure_ascii=False,
    )
    text = gemini_chat(
        [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=512,
    )
    parsed = safe_json(text) or {}

    person_terms: List[str] = []
    relative_terms: List[str] = []
    for raw in parsed.get("person_terms") or []:
        for term in _split_devanagari_terms(str(raw)):
            if term not in person_terms:
                person_terms.append(term)
    for raw in parsed.get("relative_terms") or []:
        for term in _split_devanagari_terms(str(raw)):
            if term not in relative_terms:
                relative_terms.append(term)
    return person_terms, relative_terms


def _extract_house_no(question: str) -> Optional[str]:
    patterns = [
        r"\bhouse\s*(?:number|no\.?|#)?\s*([0-9A-Za-z/-]+)",
        r"\bhouse\s+([0-9A-Za-z/-]+)",
        r"\bमकान\s*(?:नंबर|संख्या)?\s*([0-9A-Za-z/-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, question or "", flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _extract_person_and_relative(question: str) -> Tuple[str, str, Optional[str]]:
    text = question or ""
    relative_name_match = re.search(
        rf"^\s*(?:find|search(?:\s+for)?|look\s+for)?\s*(.*?)\s*,?\s*"
        rf"(?:whose\s+)?(?:relative\s+)?({RELATION_WORDS})(?:'s)?\s+name\s+"
        rf"(?:is|=)\s+([a-zA-Z ]+?)(?=\s+\b(?:house|search|in|at|from)\b|[.,;]|$)",
        text,
        flags=re.IGNORECASE,
    )
    if relative_name_match:
        person = relative_name_match.group(1).strip(" .,;:-")
        relation_word = relative_name_match.group(2).lower()
        relative = relative_name_match.group(3).strip(" .,;:-")
        return person, relative, RELATION_TYPE_BY_WORD.get(relation_word)

    relation_match = re.search(
        rf"\b({RELATION_WORDS})\s+of\s+([a-zA-Z ]+?)(?=\s+\b(?:house|search|in|at|from)\b|[.,;]|$)",
        text,
        flags=re.IGNORECASE,
    )
    if not relation_match:
        return text, "", None

    person = text[: relation_match.start()].strip(" .,;:-")
    relation_word = relation_match.group(1).lower()
    relative = relation_match.group(2).strip(" .,;:-")
    return person, relative, RELATION_TYPE_BY_WORD.get(relation_word)


def _format_voter_rows(rows: List[Dict[str, Any]], question: str) -> str:
    def row_line(row: Dict[str, Any], index: Optional[int] = None) -> str:
        prefix = f"{index}. " if index is not None else ""
        relation = row.get("relation_type") or "Relative"
        parts = [
            f"{prefix}Name: {row.get('name', '')}",
            f"Relative: {row.get('relative_name', '')} ({relation})",
            f"House number: {row.get('house_no', '')}",
            f"Age/Gender: {row.get('age', '')}, {row.get('gender', '')}",
            f"Polling station: {row.get('polling_station', '')}",
            f"Region: {row.get('region', '')}",
            f"EPIC: {row.get('epic_id', '')}",
        ]
        return "\n".join(parts)

    if wants_hindi(question):
        if len(rows) == 1:
            row = rows[0]
            relation = row.get("relation_type") or "Relative"
            return "\n".join(
                [
                    "मिलान वाला मतदाता मिला।",
                    f"नाम: {row.get('name', '')}",
                    f"रिश्तेदार: {row.get('relative_name', '')} ({relation})",
                    f"मकान नंबर: {row.get('house_no', '')}",
                    f"उम्र/लिंग: {row.get('age', '')}, {row.get('gender', '')}",
                    f"मतदान केंद्र: {row.get('polling_station', '')}",
                    f"क्षेत्र: {row.get('region', '')}",
                    f"EPIC: {row.get('epic_id', '')}",
                ]
            )
        return "मिलान वाले मतदाता मिले।\n" + "\n\n".join(
            row_line(row, i + 1) for i, row in enumerate(rows[:5])
        )

    if len(rows) == 1:
        return "Found matching voter.\n" + row_line(rows[0])
    return "Found matching voters.\n" + "\n\n".join(row_line(row, i + 1) for i, row in enumerate(rows[:5]))


def try_direct_voter_lookup(question: str, slug: str) -> Optional[str]:
    if not re.search(r"[a-zA-Z]", question or ""):
        return None
    if not re.search(r"\b(search|find|voter|electoral|db|house|wife|husband|father|mother|son|daughter)\b", question or "", flags=re.IGNORECASE):
        return None

    person_phrase, relative_phrase, relation_type = _extract_person_and_relative(question)
    person_terms, relative_terms = _transliterate_voter_phrases(person_phrase, relative_phrase)
    house_no = _extract_house_no(question)

    if not person_terms and not relative_terms:
        return None
    if relative_phrase and not relative_terms:
        return None

    conditions = []
    params: List[Any] = []
    for term in person_terms:
        conditions.append("name LIKE ?")
        params.append(f"%{term}%")
    for term in relative_terms:
        conditions.append("relative_name LIKE ?")
        params.append(f"%{term}%")
    if house_no:
        conditions.append("TRIM(CAST(house_no AS TEXT)) = ?")
        params.append(house_no)
    if relation_type:
        conditions.append("relation_type = ?")
        params.append(relation_type)

    sql = """
        SELECT serial_no, name, relative_name, relation_type, house_no, age, gender,
               epic_id, polling_station, caste, social_category, religion, region
        FROM voters
        WHERE {where}
        ORDER BY polling_station, serial_no
        LIMIT 10
    """.format(where=" AND ".join(conditions))

    try:
        conn = gcs_data.open_sqlite(slug, "voters.db")
        conn.row_factory = sqlite3.Row
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        conn.close()
    except Exception:
        return None

    if not rows:
        return None
    return _format_voter_rows(rows, question)


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

    direct_answer = try_direct_voter_lookup(question, slug)
    if direct_answer:
        return direct_answer

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
    blocks = "\n\n".join(
        f"Internal calculation:\n{s.action_input}\nVerified data:\n{s.observation[:3000]}"
        for s in good
    )
    return gemini_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are preparing the final answer for a campaign user. "
                    "Use only the verified data. Do not invent numbers.\n\n"
                    + answer_style_instruction(question)
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Private work material. Do not mention this material in the answer:\n{blocks}\n\n"
                    "Write the final user-facing answer."
                ),
            },
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
    blocks = "\n\n".join(
        f"Internal calculation:\n{s.action_input}\nVerified data:\n{s.observation[:3500]}"
        for s in good
    )
    return gemini_chat(
        [
            {
                "role": "system",
                "content": (
                    "You are preparing the final answer for a campaign user. "
                    "Use only the verified election data. Do not invent numbers.\n\n"
                    + answer_style_instruction(question)
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\n"
                    f"Private work material. Do not mention this material in the answer:\n{blocks}\n\n"
                    "Write the final user-facing answer."
                ),
            },
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
                + "\n\n"
                + answer_style_instruction(question)
                + "\n\nWrite the final user-facing answer from the portfolio data only.",
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
        evidence = "\n\n".join(f"{name}\n{ans}" for name, ans in results)
        answer = gemini_chat(
            [
                {
                    "role": "system",
                    "content": system + "\n\n" + answer_style_instruction(question),
                },
                {
                    "role": "user",
                    "content": f"Question: {question}\n\nSpecialist findings:\n{evidence}\n\n"
                    "Write one clear final answer for the campaign user. "
                    "Do not invent numbers or mention internal process.",
                },
            ],
            temperature=0.1,
        )

    answer = clean_user_answer(answer, question)
    return {
        "answer": answer,
        "charts": [],
        "session_id": session_id,
        "agent_path": agent_path,
    }
