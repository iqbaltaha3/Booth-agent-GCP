"""Validated messages exchanged by the coordinator and its specialists."""

from typing import Any, Literal
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NameQuery(Contract):
    operation: Literal["count", "lookup"]
    person: str = Field(default="", max_length=200)
    relative: str = Field(default="", max_length=200)
    match_mode: Literal["word", "full", "substring"] = "word"
    # Exact roll columns only. A booth identifier is resolved separately.
    booth: str | None = Field(default=None, max_length=500)
    filters: dict[str, str | int | float] = Field(default_factory=dict, max_length=20)
    # Populate only after the user explicitly chooses these Hindi spellings.
    confirmed_person_variants: list[str] = Field(default_factory=list, max_length=8)
    confirmed_relative_variants: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def has_name(self):
        if not self.person.strip() and not self.relative.strip():
            raise ValueError("A person or relative name is required")
        if self.booth is not None and not self.booth.strip():
            raise ValueError("booth cannot be blank")
        return self


class HouseholdQuery(Contract):
    # reference is an opaque server-issued key, never an address reconstructed by the model.
    reference: str | None = None
    epic_id: str | None = None
    house_no: str | None = None
    polling_station: str | None = None
    exclude_anchor: bool = False

    @model_validator(mode="after")
    def target(self):
        anchors = int(bool(self.reference)) + int(bool(self.epic_id))
        if anchors > 1 or (anchors and (self.house_no or self.polling_station)):
            raise ValueError("Use one reference/EPIC or an explicit house and station")
        if not anchors and not (self.house_no and self.polling_station):
            raise ValueError("Household lookup needs a reference/EPIC or house plus station")
        if self.exclude_anchor and not anchors:
            raise ValueError("exclude_anchor requires a voter reference or EPIC")
        return self


class WebResearchQuery(Contract):
    public_question: str = Field(min_length=3, max_length=1500)
    locality: str | None = Field(default=None, max_length=200)
    scope: Literal["constituency", "district", "locality"] = "constituency"
    time_sensitive: bool = True
    start_date: date | None = None
    end_date: date | None = None
    max_pages: int = Field(default=6, ge=1, le=8)

    @model_validator(mode="after")
    def window(self):
        if bool(self.start_date) != bool(self.end_date):
            raise ValueError("Supply both start_date and end_date or neither")
        if self.start_date and self.start_date > self.end_date:
            raise ValueError("start_date must precede end_date")
        if self.scope == "locality" and not self.locality:
            raise ValueError("Locality scope requires a named locality")
        return self


class SpecialistTask(Contract):
    task_id: str = Field(min_length=1, max_length=100)
    agent: Literal["voter", "history", "portfolio", "research"]
    objective: str = Field(min_length=1, max_length=4000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    required_outputs: list[str] = Field(min_length=1, max_length=20)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    name_query: NameQuery | None = None
    household_query: HouseholdQuery | None = None
    research_query: WebResearchQuery | None = None

    @model_validator(mode="after")
    def name_owner(self):
        if (self.name_query is not None or self.household_query is not None) and self.agent != "voter":
            raise ValueError("Only voter tasks accept name_query/household_query")
        if self.name_query and self.household_query:
            raise ValueError("A task cannot combine name and household queries")
        if (self.agent == "research") != (self.research_query is not None):
            raise ValueError("research tasks require research_query; other agents cannot use it")
        if self.research_query and self.constraints:
            raise ValueError("Research constraints belong in research_query")
        return self


class SpecialistResult(Contract):
    task_id: str
    agent: Literal["voter", "history", "portfolio", "research"]
    status: Literal["complete", "partial", "needs_clarification", "failed"]
    facts: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class CoordinatorAction(Contract):
    action: Literal["delegate", "clarify", "finish"]
    tasks: list[SpecialistTask] = Field(default_factory=list, max_length=3)
    answer: str = ""
    status: Literal["complete", "partial", "failed"] = "complete"
    # A final data answer must cite completed observations internally.
    evidence_task_ids: list[str] = Field(default_factory=list)
    selected_voter_reference: str | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "delegate":
            if not self.tasks or self.answer or self.evidence_task_ids:
                raise ValueError("delegate requires tasks only")
            if len({t.task_id for t in self.tasks}) != len(self.tasks):
                raise ValueError("Task IDs must be unique")
        elif not self.answer.strip() or self.tasks:
            raise ValueError("clarify/finish requires an answer and no tasks")
        return self


class ResearchAction(Contract):
    action: Literal["sql", "finish", "clarify"]
    sql: str = ""
    status: Literal["complete", "partial", "failed"] = "complete"
    missing_inputs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action == "sql" and not self.sql.strip():
            raise ValueError("sql action requires SQL")
        if self.action == "clarify" and not self.missing_inputs:
            raise ValueError("clarify requires missing_inputs")
        return self


class NameCandidates(Contract):
    variants: list[str] = Field(min_length=1, max_length=8)
