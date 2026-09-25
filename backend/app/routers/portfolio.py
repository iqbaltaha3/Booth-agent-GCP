"""
Booth-portfolio routes.  Called by the ADK PortfolioSubAgent.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import get_current_user, require_constituency
from app.schemas.portfolio import (
    BoothListResponse,
    BoothOption,
    BoothRegion,
    PortfolioResponse,
    PortfolioSearchResponse,
)
from app.services import gcs_data

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def _norm_part(s: str) -> Optional[str]:
    m = re.search(r"(\d+)", s)
    if m:
        return f"Part_{int(m.group(1))}"
    return None


def _row_booth_name(row: Dict[str, Any]) -> str:
    return (
        row.get("pooling_station")
        or row.get("polling_station")
        or row.get("booth_name")
        or ""
    ).strip()


def _row_part(row: Dict[str, Any]) -> str:
    return str(row.get("part_no") or row.get("part_number") or "").strip()


def _row_region(row: Dict[str, Any]) -> str:
    return str(row.get("region") or "Unassigned").strip() or "Unassigned"


def _row_total_voters(row: Dict[str, Any]) -> Optional[float]:
    raw = str(row.get("total_voters") or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _part_sort_value(part_number: str) -> int:
    m = re.search(r"\d+", part_number or "")
    return int(m.group(0)) if m else 999999


@router.get("/booths", response_model=BoothListResponse)
def list_booths(
    constituency_id: str = Query(...),
    user: Dict[str, Any] = Depends(get_current_user),
) -> BoothListResponse:
    slug = require_constituency(constituency_id, user)
    try:
        rows = gcs_data.load_boothlist(slug)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    by_region: Dict[str, List[BoothOption]] = {}
    for row in rows:
        part = _row_part(row)
        booth_name = _row_booth_name(row)
        if not part and not booth_name:
            continue
        region = _row_region(row)
        by_region.setdefault(region, []).append(
            BoothOption(
                part_number=part,
                booth_name=booth_name or part,
                region=region,
                total_voters=_row_total_voters(row),
            )
        )

    return BoothListResponse(
        regions=[
            BoothRegion(
                region=region,
                booths=sorted(booths, key=lambda b: _part_sort_value(b.part_number)),
            )
            for region, booths in sorted(by_region.items(), key=lambda item: item[0].lower())
        ]
    )


@router.get("/search", response_model=PortfolioSearchResponse)
def search_booths(
    q: str = Query(..., min_length=1),
    constituency_id: str = Query(...),
    user: Dict[str, Any] = Depends(get_current_user),
) -> PortfolioSearchResponse:
    slug = require_constituency(constituency_id, user)
    try:
        rows = gcs_data.load_boothlist(slug)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    stations = []
    for r in rows:
        name = _row_booth_name(r)
        part = _row_part(r)
        if name:
            stations.append({"booth_name": name, "part_number": part, "row": r})

    # Fuzzy match
    names = [s["booth_name"] for s in stations]
    close = difflib.get_close_matches(q, names, n=8, cutoff=0.4)
    # Also try part-number match
    part_q = _norm_part(q)
    matches: List[Dict[str, Any]] = []
    seen = set()
    for s in stations:
        if part_q and s["part_number"].lower() == part_q.lower():
            key = s["booth_name"]
            if key not in seen:
                matches.append(
                    {"booth_name": s["booth_name"], "part_number": s["part_number"]}
                )
                seen.add(key)
    for name in close:
        if name not in seen:
            s = next(x for x in stations if x["booth_name"] == name)
            matches.append(
                {"booth_name": s["booth_name"], "part_number": s["part_number"]}
            )
            seen.add(name)

    return PortfolioSearchResponse(matches=matches[:10])


@router.get("/{booth_identifier}", response_model=PortfolioResponse)
def get_portfolio(
    booth_identifier: str,
    constituency_id: str = Query(...),
    user: Dict[str, Any] = Depends(get_current_user),
) -> PortfolioResponse:
    slug = require_constituency(constituency_id, user)

    # Try to resolve via boothlist first
    booth_name = booth_identifier
    part_number = _norm_part(booth_identifier)
    try:
        rows = gcs_data.load_boothlist(slug)
        for r in rows:
            name = _row_booth_name(r)
            part = _row_part(r)
            if part_number and part.lower() == part_number.lower():
                booth_name = name or booth_name
                part_number = part
                break
            if name.lower() == booth_identifier.lower():
                booth_name = name
                part_number = part or part_number
                break
    except FileNotFoundError:
        pass

    # Find a matching JSON file
    files = gcs_data.list_portfolio_files(slug)
    target = None

    if part_number:
        for f in files:
            try:
                candidate = gcs_data.load_portfolio_json(slug, f)
            except FileNotFoundError:
                continue
            meta = candidate.get("booth_metadata") or {}
            if str(meta.get("booth_name") or "").strip().lower() == part_number.lower():
                target = f
                break

    # Exact basename match
    if not target:
        for f in files:
            if booth_identifier.lower() in f.lower() or (
                booth_name and booth_name.lower().replace(" ", "_") in f.lower()
            ):
                target = f
                break
    if not target and files:
        # Fuzzy on filenames
        close = difflib.get_close_matches(
            booth_identifier.replace(" ", "_"),
            [f.replace("_stats.json", "").replace(".json", "") for f in files],
            n=1,
            cutoff=0.4,
        )
        if close:
            for f in files:
                if close[0] in f:
                    target = f
                    break

    if not target:
        raise HTTPException(
            status_code=404,
            detail=f"No portfolio found for '{booth_identifier}'",
        )

    try:
        data = gcs_data.load_portfolio_json(slug, target)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return PortfolioResponse(
        booth_name=booth_name,
        part_number=part_number,
        portfolio=data,
        source_file=target,
    )
