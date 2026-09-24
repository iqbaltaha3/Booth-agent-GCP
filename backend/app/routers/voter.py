"""
Voter-roll routes.  Called by the ADK VoterSubAgent as tools.
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

from app.deps import get_current_user, require_constituency
from app.schemas.voter import SchemaResponse, VoterQueryRequest, VoterQueryResponse
from app.services import gcs_data

router = APIRouter(prefix="/voter", tags=["voter"])


@router.post("/query", response_model=VoterQueryResponse)
def query_voters(
    body: VoterQueryRequest,
    user: Dict[str, Any] = Depends(get_current_user),
) -> VoterQueryResponse:
    slug = require_constituency(body.constituency_id, user)
    try:
        conn = gcs_data.open_sqlite(slug, "voters.db")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        result = gcs_data.execute_select(conn, body.sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()
    return VoterQueryResponse(**result)


@router.get("/schema", response_model=SchemaResponse)
def voter_schema(
    constituency_id: str,
    user: Dict[str, Any] = Depends(get_current_user),
) -> SchemaResponse:
    slug = require_constituency(constituency_id, user)
    try:
        conn = gcs_data.open_sqlite(slug, "voters.db")
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    try:
        tables = gcs_data.list_tables(conn)
        lines = []
        samples: Dict[str, Any] = {}
        for t in tables:
            cols = gcs_data.list_columns(conn, t)
            lines.append(f"TABLE {t}: {', '.join(cols)}")
            if t == "voters":
                for col in ("region", "caste", "religion", "gender", "social_category"):
                    if col in cols:
                        samples[col] = gcs_data.sample_values(conn, t, col, limit=30)
        return SchemaResponse(
            tables=tables,
            schema_text="\n".join(lines) or "(no tables)",
            samples=samples,
        )
    finally:
        conn.close()
