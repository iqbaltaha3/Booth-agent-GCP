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
