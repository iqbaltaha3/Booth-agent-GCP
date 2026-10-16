"""Validated messages exchanged by the coordinator and its specialists."""

from typing import Any, Literal

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


class SpecialistTask(Contract):
    task_id: str = Field(min_length=1, max_length=100)
    agent: Literal["voter", "history", "portfolio"]
    objective: str = Field(min_length=1, max_length=4000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    required_outputs: list[str] = Field(min_length=1, max_length=20)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    name_query: NameQuery | None = None

    @model_validator(mode="after")
    def name_owner(self):
        if self.name_query is not None and self.agent != "voter":
            raise ValueError("Only voter tasks accept name_query")
        return self


class SpecialistResult(Contract):
    task_id: str
    agent: Literal["voter", "history", "portfolio"]
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
