"""Authenticated structured lookup with private, bounded responses."""

import logging
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from agents.execution import execution_scope
from app.auth import get_current_user, require_constituency
from app.services import voter_search
from app.voter_search_models import SearchOptions, SearchRequest, SearchResponse

router = APIRouter(prefix="/voter", tags=["voter-search"])
logger = logging.getLogger(__name__)


def _run(operation):
    request_id = uuid4().hex
    headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
    try:
        # The shared SQLite/GCS helpers reserve 15 seconds from this budget.
        with execution_scope(seconds=60):
            result = operation()
        if isinstance(result, SearchResponse):
            result.request_id = request_id
        return JSONResponse(result.model_dump(mode="json"), headers=headers)
    except voter_search.SearchError as error:
        code, status = error.code, error.status
    except FileNotFoundError:
        code, status = "data_unavailable", 503
    except TimeoutError:
        code, status = "deadline_exceeded", 504
    except sqlite3.OperationalError as error:
        code, status = ("deadline_exceeded", 504) if "interrupted" in str(error) else ("search_unavailable", 503)
    except Exception:
        # Never log query details, records, provider bodies, or exception messages.
        logger.error("Voter search failed request_id=%s", request_id)
        code, status = "search_unavailable", 503
    return JSONResponse({"error_code": code, "request_id": request_id}, status_code=status, headers=headers)


@router.get("/search-options", response_model=SearchOptions)
def options(constituency_id: str = Query(...), user: dict = Depends(get_current_user)):
    slug = require_constituency(constituency_id, user)
    return _run(lambda: voter_search.get_options(slug))


@router.post("/search", response_model=SearchResponse)
def search(body: SearchRequest, user: dict = Depends(get_current_user)):
    slug = require_constituency(body.constituency_id, user)
    return _run(lambda: voter_search.search(body, slug))
