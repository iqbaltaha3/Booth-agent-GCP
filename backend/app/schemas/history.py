from __future__ import annotations

from typing import Any, List

from pydantic import BaseModel, Field


class HistoryQueryRequest(BaseModel):
    sql: str
    constituency_id: str


class HistoryQueryResponse(BaseModel):
    columns: List[str]
    rows: List[List[Any]]
    row_count: int
    truncated: bool


class HistorySchemaResponse(BaseModel):
    tables: List[str]
    schema_text: str
    samples: dict
