from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class PortfolioResponse(BaseModel):
    booth_name: Optional[str] = None
    part_number: Optional[str] = None
    portfolio: Dict[str, Any]
    source_file: Optional[str] = None


class PortfolioSearchResponse(BaseModel):
    matches: List[Dict[str, Any]]


class BoothOption(BaseModel):
    part_number: str
    booth_name: str
    region: str
    total_voters: Optional[float] = None


class BoothRegion(BaseModel):
    region: str
    booths: List[BoothOption]


class BoothListResponse(BaseModel):
    regions: List[BoothRegion]
