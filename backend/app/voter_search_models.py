"""Public contracts for structured voter lookup; no SQL or chat state accepted."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    constituency_id: str = Field(min_length=1, max_length=100)
    region: str | None = Field(default=None, max_length=500)
    booth_id: str | None = Field(default=None, max_length=100)
    name: str = Field(default="", max_length=200)
    relative_name: str = Field(default="", max_length=200)
    house_no: str = Field(default="", max_length=100)
    epic_id: str = Field(default="", max_length=100)
    search_mode: Literal["standard", "name_only", "relative_only"] = "standard"
    spelling_assistance: bool = False
    page_size: int = Field(default=20, ge=1, le=40)

    @field_validator("name", "relative_name", "house_no", "epic_id", mode="before")
    @classmethod
    def trim_details(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("region", "booth_id", mode="before")
    @classmethod
    def blank_scope(cls, value):
        return value.strip() or None if isinstance(value, str) else value

    @model_validator(mode="after")
    def sufficient_details(self):
        if self.search_mode != "standard":
            field = self.name if self.search_mode == "name_only" else self.relative_name
            if not self.booth_id or not field:
                raise ValueError("A single-name search requires a specific booth and that name")
        elif not self.epic_id:
            count = sum(bool(v) for v in (self.name, self.relative_name, self.house_no))
            if count < 2 and not (self.booth_id and (self.name or self.relative_name)):
                raise ValueError("Enter EPIC alone, two identifying details, or a name and a specific booth")
        return self


class SearchCoverage(BaseModel):
    availability: Literal["available", "unavailable", "unknown"]
    completeness: Literal["partial", "declared", "unverified"]


class SearchBooth(BaseModel):
    booth_id: str
    part_number: str
    booth_name: str
    region: str
    mapping_status: Literal["mapped", "unmapped", "ambiguous"]


class SearchOptions(BaseModel):
    constituency_id: str
    coverage: SearchCoverage
    regions: list[str] = Field(default_factory=list)
    booths: list[SearchBooth] = Field(default_factory=list)
    source_version: str | None = None
    booth_list_available: bool = True


MatchStatus = Literal["exact", "name_words", "spelling_variant", "different", "not_recorded"]


class VoterDetails(BaseModel):
    serial_no: str | None = None
    name: str | None = None
    relative_name: str | None = None
    relation_type: str | None = None
    house_no: str | None = None
    age: str | None = None
    gender: str | None = None
    epic_id: str | None = None
    region: str | None = None
    polling_station: str | None = None


class SearchCandidate(BaseModel):
    record_id: str
    voter: VoterDetails
    booth_id: str | None = None
    match_tier: Literal["epic_exact", "all_details", "partial_name", "partial_details", "single_name"]
    field_matches: dict[str, MatchStatus]
    matched_fields: int
    supplied_fields: int


class SearchResponse(BaseModel):
    constituency_id: str
    region: str | None
    booth_id: str | None
    coverage: SearchCoverage
    source_version: str | None = None
    outcome: Literal["matches", "no_matches", "refine_search", "unavailable"]
    candidates: list[SearchCandidate] = Field(default_factory=list)
    truncated: bool = False
    duplicate_epic: bool = False
    suggested_modes: list[Literal["name_only", "relative_only"]] = Field(default_factory=list)
    spelling_status: Literal["not_requested", "used", "unavailable", "not_needed"] = "not_requested"
    spelling_variants: dict[str, list[str]] = Field(default_factory=dict)
    request_id: str = ""
