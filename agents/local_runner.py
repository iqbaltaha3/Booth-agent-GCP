"""
LangGraph entry point and shared prompt/presentation helpers.

Gemini calls use LangChain through agents.model in every environment.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
AGENTS_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = AGENTS_DIR / "prompts"
REPO_ROOT = AGENTS_DIR.parent

# Ensure backend package is importable when running from repo root
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.services import data as gcs_data  # noqa: E402
from agents import execution


# Stable model boundary used by the specialists and evaluation harness.
from agents.model import chat as gemini_chat

__all__ = [
    "gcs_data",
    "gemini_chat",
    "run_local_agent",
    "safe_json",
    "load_prompt",
    "load_constituency_meta",
    "wrap_electoral",
    "wants_hindi",
    "answer_style_instruction",
    "clean_user_answer",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def safe_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    text = text.strip()
    for candidate in (
        text,
        re.sub(r"```(?:json)?", "", text, flags=re.I).replace("```", "").strip(),
    ):
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


def load_constituency_meta(slug: str) -> dict[str, str]:
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
) -> dict[str, Any]:
    """Run the LangGraph harness with server-owned conversation state."""
    from agents.runtime import run_coordinator

    with execution.execution_scope(
        max(30, min(int(os.environ.get("AGENT_TIMEOUT_SECONDS", "240")), 540))
    ):
        return run_coordinator(
            question,
            constituency_id,
            session_id,
            history,
            pending_state,
            runner=sys.modules[__name__],
            voter_context=voter_context,
        )
