"""
Election-history (Form-20) routes.  Called by the ADK HistorySubAgent.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from app.deps import get_current_user, require_constituency
from app.schemas.history import (
    HistoryQueryRequest,
    HistoryQueryResponse,
    HistorySchemaResponse,
)
from app.services import gcs_data

router = APIRouter(prefix="/history", tags=["history"])


@router.post("/query", response_model=HistoryQueryResponse)
def query_history(
    body: HistoryQueryRequest,
    user: Dict[str, Any] = Depends(get_current_user),
) -> HistoryQueryResponse:
    slug = require_constituency(body.constituency_id, user)
    try:
        conn = gcs_data.open_sqlite(slug, "history.db")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        result = gcs_data.execute_select(conn, body.sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()
    return HistoryQueryResponse(**result)


@router.get("/schema", response_model=HistorySchemaResponse)
def history_schema(
    constituency_id: str,
    user: Dict[str, Any] = Depends(get_current_user),
) -> HistorySchemaResponse:
    slug = require_constituency(constituency_id, user)
    try:
        conn = gcs_data.open_sqlite(slug, "history.db")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        tables = gcs_data.list_tables(conn)
        lines = []
        samples: Dict[str, Any] = {}
        for t in tables:
            cols = gcs_data.list_columns(conn, t)
            lines.append(f"TABLE {t}: {', '.join(cols)}")
            entry: Dict[str, Any] = {}
            if "party" in cols:
                entry["party"] = gcs_data.sample_values(conn, t, "party", limit=25)
            if "area" in cols:
                entry["area"] = gcs_data.sample_values(conn, t, "area", limit=25)
            if entry:
                samples[t] = entry
        return HistorySchemaResponse(
            tables=tables,
            schema_text="\n".join(lines) or "(no tables)",
            samples=samples,
        )
    finally:
        conn.close()
