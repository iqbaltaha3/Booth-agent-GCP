"""
Per-constituency data access.

In production the backend opens SQLite / JSON / CSV only from the
validated GCS folder that belongs to the authenticated user's chosen
constituency.  The path is never taken from the agent or the client.

Local development: set LOCAL_DATA_ROOT to a folder that mirrors the
GCS layout (e.g. legacy-data/) so you can run without a bucket.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import get_settings
from app.services.sql_safety import quote_ident, validate_select

logger = logging.getLogger(__name__)

# Short-lived local cache of downloaded SQLite files (key = slug/filename)
_CACHE: Dict[str, Path] = {}
_CACHE_DIR = Path(tempfile.gettempdir()) / "booth-agent-gcs-cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _local_root() -> Optional[Path]:
    settings = get_settings()
    if settings.local_data_root:
        return Path(settings.local_data_root)
    # Fallback: look for legacy-data next to the repo root
    candidates = [
        Path.cwd() / "legacy-data",
        Path(__file__).resolve().parents[3] / "legacy-data",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _gcs_client():
    try:
        from google.cloud import storage
        return storage.Client()
    except Exception as e:
        logger.warning("GCS client unavailable: %s", e)
        return None


def resolve_data_path(slug: str, filename: str) -> Path:
    """
    Return a local filesystem path to the requested file for `slug`.
    Prefers local_data_root when set; otherwise downloads from GCS into a
    temp cache.
    """
    slug = slug.strip().lower()
    if not slug or ".." in slug or "/" in slug or "\\" in slug:
        raise ValueError(f"Invalid constituency slug: {slug!r}")

    local = _local_root()
    if local is not None:
        path = local / slug / filename
        if path.exists():
            return path
        # also try flat layout (legacy single-constituency)
        flat = local / filename
        if flat.exists():
            return flat
        raise FileNotFoundError(
            f"Local data missing: {path} (and {flat})"
        )

    # GCS path
    settings = get_settings()
    cache_key = f"{slug}/{filename}"
    if cache_key in _CACHE and _CACHE[cache_key].exists():
        return _CACHE[cache_key]

    client = _gcs_client()
    if client is None:
        raise RuntimeError(
            "GCS client not available and LOCAL_DATA_ROOT is not set. "
            "For local dev, place data under legacy-data/<slug>/ and set LOCAL_DATA_ROOT."
        )

    bucket = client.bucket(settings.gcs_data_bucket)
    blob = bucket.blob(f"{slug}/{filename}")
    if not blob.exists():
        raise FileNotFoundError(
            f"gs://{settings.gcs_data_bucket}/{slug}/{filename} not found"
        )

    dest = _CACHE_DIR / slug
    dest.mkdir(parents=True, exist_ok=True)
    local_path = dest / filename
    blob.download_to_filename(str(local_path))
    _CACHE[cache_key] = local_path
    logger.info("Downloaded gs://%s/%s/%s → %s", settings.gcs_data_bucket, slug, filename, local_path)
    return local_path


def open_sqlite(slug: str, filename: str) -> sqlite3.Connection:
    path = resolve_data_path(slug, filename)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def list_tables(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def list_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    tq = quote_ident(table)
    rows = conn.execute(f"PRAGMA table_info({tq})").fetchall()
    return [r["name"] for r in rows]


def sample_values(
    conn: sqlite3.Connection, table: str, column: str, limit: int = 40
) -> List[str]:
    tq, cq = quote_ident(table), quote_ident(column)
    rows = conn.execute(
        f"""
        SELECT DISTINCT {cq} FROM {tq}
        WHERE {cq} IS NOT NULL AND TRIM(CAST({cq} AS TEXT)) != ''
        ORDER BY {cq} LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [str(r[0]).strip() for r in rows if str(r[0]).strip()]


def execute_select(
    conn: sqlite3.Connection,
    sql: str,
    max_rows: int = 80,
) -> Dict[str, Any]:
    ok, cleaned = validate_select(sql)
    if not ok:
        raise ValueError(cleaned)
    cur = conn.execute(cleaned)
    rows = cur.fetchmany(max_rows)
    columns = [d[0] for d in cur.description] if cur.description else []
    data = [list(r) for r in rows]
    truncated = len(data) >= max_rows
    return {
        "columns": columns,
        "rows": data,
        "row_count": len(data),
        "truncated": truncated,
    }


def load_boothlist(slug: str) -> List[Dict[str, Any]]:
    path = resolve_data_path(slug, "boothlist.csv")
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def load_portfolio_json(slug: str, filename: str) -> Dict[str, Any]:
    path = resolve_data_path(slug, f"booth_analysis/{filename}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def list_portfolio_files(slug: str) -> List[str]:
    """Return basenames of *.json under booth_analysis/ for the slug."""
    local = _local_root()
    if local is not None:
        d = local / slug / "booth_analysis"
        if not d.exists():
            d = local / "booth_analysis"
        if d.exists():
            return sorted(p.name for p in d.glob("*.json"))
        return []

    settings = get_settings()
    client = _gcs_client()
    if client is None:
        return []
    bucket = client.bucket(settings.gcs_data_bucket)
    prefix = f"{slug}/booth_analysis/"
    return sorted(
        blob.name.split("/")[-1]
        for blob in bucket.list_blobs(prefix=prefix)
        if blob.name.endswith(".json")
    )
