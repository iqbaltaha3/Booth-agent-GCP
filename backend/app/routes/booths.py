"""
Booth-portfolio routes.  Called by the ADK PortfolioSubAgent.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import get_current_user, require_constituency
from app.models import (
    BoothListResponse,
    BoothOption,
    BoothRegion,
    PortfolioResponse,
    PortfolioSearchResponse,
)
from app.services import data

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def _norm_part(s: str) -> str | None:
    """Convert a booth number to the standard Part_123 form."""
    m = re.search(r"(\d+)", s)
    if m:
        return f"Part_{int(m.group(1))}"
    return None


def _part_number(booth: BoothOption) -> int:
    """Sort booth labels by their numeric part instead of alphabetically."""
    match = re.search(r"\d+", booth.part_number)
    return int(match.group()) if match else 999999


def _booths(slug: str) -> list[BoothOption]:
    """Normalize the existing CSV column names once for all booth endpoints."""
    try:
        rows = data.load_boothlist(slug)
    except FileNotFoundError as error:
        raise HTTPException(404, "Booth list not found") from error
    booths = []
    for row in rows:
        part = str(row.get("part_no") or row.get("part_number") or "").strip()
        name = (
            row.get("pooling_station") or row.get("polling_station") or row.get("booth_name") or ""
        ).strip()
        if not part and not name:
            continue
        try:
            voters = float(row["total_voters"])
        except (KeyError, ValueError, TypeError):
            voters = None
        booths.append(
            BoothOption(
                part_number=part,
                booth_name=name or part,
                region=(row.get("region") or "Unassigned").strip() or "Unassigned",
                total_voters=voters,
            )
        )
    return booths


@router.get("/booths", response_model=BoothListResponse)
def list_booths(
    constituency_id: str = Query(...),
    user: dict[str, Any] = Depends(get_current_user),
) -> BoothListResponse:
    """Group booth options by region for the report selector."""
    slug = require_constituency(constituency_id, user)
    by_region = {}
    for booth in _booths(slug):
        by_region.setdefault(booth.region, []).append(booth)

    return BoothListResponse(
        regions=[
            BoothRegion(
                region=region,
                booths=sorted(booths, key=_part_number),
            )
            for region, booths in sorted(by_region.items(), key=lambda item: item[0].lower())
        ]
    )


@router.get("/search", response_model=PortfolioSearchResponse)
def search_booths(
    q: str = Query(..., min_length=1),
    constituency_id: str = Query(...),
    user: dict[str, Any] = Depends(get_current_user),
) -> PortfolioSearchResponse:
    """Find booths by number or approximate station name."""
    slug = require_constituency(constituency_id, user)
    booths = _booths(slug)
    close = difflib.get_close_matches(q, [booth.booth_name for booth in booths], n=8, cutoff=0.4)
    part = _norm_part(q)
    # Prefer numeric matches, then approximate station names; preserve distinct booths.
    matches = [booth for booth in booths if part and _norm_part(booth.part_number) == part]
    for name in close:
        matches.extend(booth for booth in booths if booth.booth_name == name and booth not in matches)
    return PortfolioSearchResponse(
        matches=[{"booth_name": booth.booth_name, "part_number": booth.part_number} for booth in matches[:10]]
    )


@router.get("/{booth_identifier}", response_model=PortfolioResponse)
def get_portfolio(
    booth_identifier: str,
    constituency_id: str = Query(...),
    user: dict[str, Any] = Depends(get_current_user),
) -> PortfolioResponse:
    """Resolve a booth identifier and return its prepared report."""
    slug = require_constituency(constituency_id, user)

    # Try to resolve via boothlist first
    booth_name = booth_identifier
    part_number = _norm_part(booth_identifier)
    try:
        for booth in _booths(slug):
            if (
                part_number and _norm_part(booth.part_number) == part_number
            ) or booth.booth_name.lower() == booth_identifier.lower():
                booth_name = booth.booth_name
                part_number = _norm_part(booth.part_number) or part_number
                break
    except HTTPException as error:
        if error.status_code != 404:
            raise

    # Find a matching JSON file
    files = data.list_portfolio_files(slug)
    target = None

    if part_number:
        for f in files:
            try:
                candidate = data.load_portfolio_json(slug, f)
            except FileNotFoundError:
                continue
            meta = candidate.get("booth_metadata") or {}
            if str(meta.get("booth_name") or "").strip().lower() == part_number.lower():
                target = f
                break

    # Prefer a matching filename before trying approximate names.
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
        report = data.load_portfolio_json(slug, target)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return PortfolioResponse(
        booth_name=booth_name,
        part_number=part_number,
        portfolio=report,
        source_file=target,
    )
