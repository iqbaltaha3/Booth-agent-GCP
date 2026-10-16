"""
Validate generated queries before running them on a read-only connection.
"""

from __future__ import annotations

import re


def quote_ident(name: str) -> str:
    """Quote a simple table or column name after checking its characters."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Unsafe identifier: {name}")
    return f'"{name}"'


def validate_select(sql: str) -> tuple[bool, str]:
    """
    Allow only a single SELECT or WITH statement.
    Reject any mutating / dangerous keywords and multi-statement payloads.
    Returns (ok, cleaned_sql_or_error_message).
    """
    if not sql:
        return False, "Empty SQL."
    cleaned = sql.strip().rstrip(";").strip()
    low = cleaned.lower().lstrip("(").strip()
    if not (low.startswith("select") or low.startswith("with")):
        return False, "Only SELECT/WITH allowed."
    forbidden = (
        "insert ",
        "update ",
        "delete ",
        "drop ",
        "alter ",
        "create ",
        "replace ",
        "attach ",
        "detach ",
        "pragma ",
        "vacuum ",
        "truncate ",
        "grant ",
        "revoke ",
    )
    full = cleaned.lower()
    for kw in forbidden:
        if kw in full:
            return False, f"Forbidden operation: {kw.strip()}"
    if ";" in cleaned:
        return False, "Multiple statements not allowed."
    return True, cleaned
