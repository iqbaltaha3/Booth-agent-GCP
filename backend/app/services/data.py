"""Read constituency SQLite, CSV, and JSON files locally or from GCS."""

import csv
import json
import sqlite3
import tempfile
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.services.sql_safety import quote_ident, validate_select


def _local_root() -> Path | None:
    """Use configured local data or the repository's development data folder."""
    root = get_settings().local_data_root
    if root:
        return Path(root)
    root = Path(__file__).resolve().parents[3] / "legacy-data"
    return root if root.exists() else None


@lru_cache
def _gcs_client():
    """Reuse Google's storage client for cloud downloads."""
    from google.cloud import storage

    return storage.Client()


def _seat(slug: str) -> str:
    """Reject directory traversal before building a local or cloud path."""
    slug = slug.strip().lower()
    if not slug or any(value in slug for value in ("..", "/", "\\")):
        raise ValueError("Invalid constituency slug")
    return slug


def resolve_data_path(slug: str, filename: str) -> Path:
    """Find a seat's file locally, or download it once into a temporary cache."""
    slug = _seat(slug)
    relative = Path(filename)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid data filename")
    root = _local_root()
    if root is not None:
        seat = (root / slug).resolve()
        path = (seat / filename).resolve()
        if not path.is_relative_to(seat):
            raise ValueError("Data file must stay inside its constituency folder")
        if not path.is_file():
            raise FileNotFoundError(f"Missing constituency file: {filename}")
        return path
    return _download(get_settings().gcs_data_bucket, slug, filename)


@lru_cache(maxsize=256)
def _download(bucket: str, slug: str, filename: str) -> Path:
    """Cache downloads for this process; restarting picks up updated data."""
    from google.api_core.exceptions import NotFound

    directory = Path(tempfile.mkdtemp(prefix="booth-agent-"))
    path = directory / Path(filename).name
    try:
        _gcs_client().bucket(bucket).blob(f"{slug}/{filename}").download_to_filename(str(path))
    except NotFound as error:
        raise FileNotFoundError(f"Missing constituency file: {filename}") from error
    return path


def open_sqlite(slug: str, filename: str) -> sqlite3.Connection:
    """Open SQLite read-only so generated SQL cannot modify the voter records."""
    path = resolve_data_path(slug, filename)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.row_factory = sqlite3.Row
    return connection


def list_tables(connection: sqlite3.Connection) -> list[str]:
    """List user tables without SQLite's internal tables."""
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in rows]


def list_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    """Read column names using SQLite's built-in schema inspection."""
    return [row["name"] for row in connection.execute(f"PRAGMA table_info({quote_ident(table)})")]


def sample_values(connection: sqlite3.Connection, table: str, column: str, limit: int = 40) -> list[str]:
    """Provide sample categories that help the AI write valid filters."""
    table, column = quote_ident(table), quote_ident(column)
    rows = connection.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL AND TRIM(CAST({column} AS TEXT)) != '' ORDER BY {column} LIMIT ?",
        (limit,),
    )
    return [str(row[0]).strip() for row in rows]


def execute_select(connection: sqlite3.Connection, sql: str, max_rows: int = 80) -> dict:
    """Validate SQL and fetch one extra row to detect truncated results."""
    valid, statement = validate_select(sql)
    if not valid:
        raise ValueError(statement)
    cursor = connection.execute(statement)
    rows = cursor.fetchmany(max_rows + 1)
    return {
        "columns": [column[0] for column in cursor.description],
        "rows": [list(row) for row in rows[:max_rows]],
        "row_count": min(len(rows), max_rows),
        "truncated": len(rows) > max_rows,
    }


def load_boothlist(slug: str) -> list[dict]:
    """Read the booth CSV with Python's standard CSV parser."""
    with resolve_data_path(slug, "boothlist.csv").open(newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


def load_portfolio_json(slug: str, filename: str) -> dict:
    """Load one prepared booth report."""
    with resolve_data_path(slug, f"booth_analysis/{filename}").open(encoding="utf-8") as file:
        return json.load(file)


def list_portfolio_files(slug: str) -> list[str]:
    """List reports inside this constituency's folder only."""
    slug = _seat(slug)
    root = _local_root()
    if root is not None:
        return sorted(path.name for path in (root / slug / "booth_analysis").glob("*.json"))
    prefix = f"{slug}/booth_analysis/"
    blobs = _gcs_client().list_blobs(get_settings().gcs_data_bucket, prefix=prefix)
    return sorted(blob.name.removeprefix(prefix) for blob in blobs if blob.name.endswith(".json"))
