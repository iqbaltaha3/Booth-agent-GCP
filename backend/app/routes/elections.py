"""Expose voter-roll and election-history tools with shared database handling."""

import sqlite3
from contextlib import closing

from fastapi import APIRouter, Depends, HTTPException

from app.auth import get_current_user, require_constituency
from app.models import (
    HistoryQueryRequest,
    HistoryQueryResponse,
    HistorySchemaResponse,
    SchemaResponse,
    VoterQueryRequest,
    VoterQueryResponse,
)
from app.services import data

router = APIRouter(tags=["elections"])


def query(body, user: dict, filename: str) -> dict:
    """Check access, run a read-only query, and always close the connection."""
    slug = require_constituency(body.constituency_id, user)
    try:
        with closing(data.open_sqlite(slug, filename)) as connection:
            return data.execute_select(connection, body.sql)
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from error
    except (ValueError, sqlite3.Error) as error:
        raise HTTPException(400, str(error)) from error


def schema(seat: str, user: dict, filename: str) -> dict:
    """Describe tables and sample categories so the AI can write queries."""
    slug = require_constituency(seat, user)
    try:
        with closing(data.open_sqlite(slug, filename)) as connection:
            tables = data.list_tables(connection)
            lines, samples = [], {}
            for table in tables:
                columns = data.list_columns(connection, table)
                lines.append(f"TABLE {table}: {', '.join(columns)}")
                # Keep the original sample shapes expected by each specialist.
                if filename == "history.db":
                    entry = {
                        column: data.sample_values(connection, table, column, 25)
                        for column in ("party", "area")
                        if column in columns
                    }
                    if entry:
                        samples[table] = entry
                elif table == "voters":
                    samples = {
                        column: data.sample_values(connection, table, column, 30)
                        for column in ("region", "caste", "religion", "gender", "social_category")
                        if column in columns
                    }
            return {"tables": tables, "schema_text": "\n".join(lines) or "(no tables)", "samples": samples}
    except FileNotFoundError as error:
        raise HTTPException(404, str(error)) from error


@router.post("/voter/query", response_model=VoterQueryResponse)
def query_voters(body: VoterQueryRequest, user: dict = Depends(get_current_user)):
    """Run a voter-roll query."""
    return query(body, user, "voters.db")


@router.post("/history/query", response_model=HistoryQueryResponse)
def query_history(body: HistoryQueryRequest, user: dict = Depends(get_current_user)):
    """Run an election-history query."""
    return query(body, user, "history.db")


@router.get("/voter/schema", response_model=SchemaResponse)
def voter_schema(constituency_id: str, user: dict = Depends(get_current_user)):
    """Describe the voter database."""
    return schema(constituency_id, user, "voters.db")


@router.get("/history/schema", response_model=HistorySchemaResponse)
def history_schema(constituency_id: str, user: dict = Depends(get_current_user)):
    """Describe the election-history database."""
    return schema(constituency_id, user, "history.db")
