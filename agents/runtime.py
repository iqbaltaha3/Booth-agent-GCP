"""LangGraph orchestration and evidence-producing domain specialists."""

import json
import copy
import logging
import re
import time
from uuid import uuid4
from contextlib import closing
from functools import lru_cache
from typing import TypedDict

from langsmith import tracing_context

from pydantic import ValidationError

from agents.contracts import (
    CoordinatorAction,
    ResearchAction,
    SpecialistResult,
    AnswerRequirements,
    VoterRequest,
    VoterFilter,
)
from agents import execution
from agents.query_validation import QueryValidationError
from agents.source_catalog import (
    voter_catalog,
    validate_demographic_intent,
    CATALOG_VERSION,
)
from agents.provider_errors import ModelFailure, failure_kind, user_error
from agents.count_answers import render_count
from agents.contracts import CountEvidence
from agents.elections import (
    electoral_request,
    election_catalog,
    validate_electoral_finish,
    catalog_evidence,
    coverage_request,
    resolve_electoral_objective,
    scope_lok_answer,
)
from datetime import datetime
from zoneinfo import ZoneInfo
from agents.presentation import partial_reason as explain_partial
from agents.completion import (
    infer_requirements,
    resolve_requirements,
    prepare_task,
    validate_clarification,
    validate_zero_claim,
    whole_constituency,
    source_policy,
    ranking_rows,
    render_ranking,
)
from agents.name_matching import resolve_booth, AmbiguousInput
from agents.households import remember_voters, data_version
from agents.research import (
    research_specialist,
    supported as research_supported,
    canonical,
    validate_public_assignment,
)
from agents.voter import (
    prepare_voter_request,
    sql_population,
    remember_population,
    validate_count_answer,
    rehydrate_selected_voter,
)

MAX_COORDINATOR_TURNS = 8
MAX_SPECIALIST_CALLS = 8
logger = logging.getLogger(__name__)


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def compact_results(results, budget=48000):
    """Keep exact small observations; signal omitted evidence rather than clipping JSON."""
    compact = {}
    for key, value in reversed(list(results.items())):
        item = {**value, "facts": value["facts"][-4:]}
        size = len(dump(item))
        if size > budget:
            item = {**item, "facts": [], "evidence_omitted": True,
                    "limitations": item.get("limitations", []) + ["Evidence exceeds context budget; request a focused aggregate or smaller result."]}
            size = len(dump(item))
        if size > budget:
            continue
        compact[key] = item
        budget -= size
    return compact


def sql_specialist(task, slug, meta, runner, question=""):
    """Return actual query observations, never model-invented facts."""
    filename = "voters.db" if task.agent == "voter" else "history.db"
    result = SpecialistResult(task_id=task.task_id, agent=task.agent, status="failed")
    try:
        with closing(runner.gcs_data.open_sqlite(slug, filename)) as conn:
            schema = []
            for table in runner.gcs_data.list_tables(conn):
                columns = runner.gcs_data.list_columns(conn, table)
                samples = {c: runner.gcs_data.sample_values(conn, table, c, 20)
                           for c in ("region", "party", "area", "gender", "caste", "social_category", "religion") if c in columns}
                schema.append({"table": table, "columns": columns, "samples": samples})
                if task.agent == "history":
                    schema[-1]["row_count"] = conn.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
            system = runner.load_prompt(f"{task.agent}_agent_system.txt", **meta)
            compiled_filters, resolved_population = None, None
            if task.voter_request:
                compiled_filters, resolved_population = sql_population(task, slug, conn, runner)

            memory = []
            last_correction = None
            for _ in range(5 if task.agent == "voter" else 6):
                if execution.remaining(reserve=15) <= 0:
                    result.errors.append("deadline_exceeded")
                    result.failure_code = "deadline_exceeded"
                    break
                conn.set_progress_handler(lambda: int(execution.remaining(reserve=15) <= 0), 10000)
                raw = runner.gemini_chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": dump({"task": task.model_dump(), "original_question": question}) + "\n" +
                     runner.wrap_electoral(dump({"schema": schema, "source_catalog": voter_catalog(slug, runner.gcs_data) if task.agent == "voter" else None, "resolved_population": resolved_population, "observations": memory[-4:]}))},
                ], temperature=0.0, deadline=execution.deadline(reserve=15), response_schema=ResearchAction.model_json_schema())
                if raw.startswith("<<LLM_ERROR"):
                    result.errors.append("The specialist model is unavailable.")
                    result.failure_code = failure_kind(raw)
                    break
                try:
                    action = ResearchAction.model_validate(runner.safe_json(raw))
                    if action.action == "clarify":
                        result.status = "needs_clarification"
                        result.missing_inputs = action.missing_inputs
                        result.limitations.extend(action.limitations)
                        return result
                    if action.action == "finish":
                        if action.evidence_source_ids:
                            known = {fact.get("source_id") for fact in result.facts}
                            if not set(action.evidence_source_ids) <= known:
                                raise ValueError("Finish evidence_source_ids must refer to executed observations")
                            final_facts = [f for f in result.facts if f.get("source_id") in action.evidence_source_ids]
                        else:
                            final_facts = result.facts
                        if task.answer_requirements and task.answer_requirements.intent == "election_ranking" and action.status == "complete" and not action.gaps:
                            ranking_rows(task.answer_requirements, [{**result.model_dump(), "facts": final_facts}], {task.task_id: task.model_dump()})
                        result.facts = final_facts
                        if task.voter_request and task.voter_request.operation == 'count' and action.status == 'complete':
                            if not any(f.get('validated_count') for f in final_facts):
                                raise ValueError('Count requests require a final executed ungrouped COUNT(*) AS total observation; label samples or record lists are insufficient.')
                        result.limitations.extend(action.limitations)
                        if any(f.get("truncated") for f in result.facts):
                            result.gaps.append("Some returned results are incomplete; they do not establish a full comparison or absence of matches.")
                        result.gaps.extend(action.gaps)
                        result.status = ("partial" if result.gaps else action.status) if result.facts else "failed"
                        if not result.facts:
                            result.errors.append("Specialist finished without successful evidence.")
                        return result
                    if task.voter_request or task.history_request:
                        from agents.query_validation import check_query
                        check_query(action.sql, task, conn, runner, compiled_filters)
                    observation = runner.gcs_data.execute_select(conn, action.sql)
                    if task.voter_request:
                        from agents.query_validation import count_observation
                        observation = count_observation(action.sql, observation)
                        if task.voter_request.operation == 'count' and observation.get('validated_count'):
                            observation['count_scope'] = CountEvidence(total=observation['total'], constituency_id=slug,
                                data_version=data_version(slug, runner.gcs_data),
                                **{k: resolved_population[k] for k in ('filters', 'any_of', 'person', 'relative', 'match_mode')}).model_dump()
                        if task.voter_request.operation != 'count':
                            observation.pop('validated_count', None)
                        if task.voter_request.operation in {'lookup', 'household'} and 'epic_id' in observation.get('columns', []):
                            observation['records'] = [dict(zip(observation['columns'], row)) for row in observation['rows']]
                            if task.voter_request.operation == 'household':
                                observation['operation'] = 'household_lookup'
                    source_id = f"{task.task_id}:q{len(result.sources) + 1}"
                    result.sources.append({"source_id": source_id, "database": filename, "sql": action.sql})
                    result.facts.append({"source_id": source_id, **observation})
                    memory.append({"sql": action.sql, "result": observation})
                    result.failure_code = None
                    if observation["truncated"]:
                        result.limitations.append(f"{source_id}: rows truncated to 80; use aggregation for totals.")
                except Exception as error:
                    # Corrections consume the same bounded specialist budget.
                    memory.append({"error": str(error)})
                    result.failure_code = "query_validation_failed" if isinstance(error, QueryValidationError) else None
                    if str(error) == last_correction:
                        break
                    last_correction = str(error)
            result.status = "partial" if result.facts else "failed"
            result.errors.extend(item["error"] for item in memory[-3:] if "error" in item)
            result.limitations.append("Specialist turn limit reached or model unavailable before completion.")
            if not result.facts:
                result.errors.append("No successful query evidence; inspect task constraints or schema.")
    except Exception as error:
        if isinstance(error, ModelFailure):
            result.failure_code = error.kind
        if isinstance(error, AmbiguousInput):
            result.status = 'needs_clarification'
            result.missing_inputs = [str(error)]
        else:
            result.errors.append(str(error))
        if isinstance(error, FileNotFoundError):
            result.failure_code = "data_unavailable"
    return result


def portfolio_specialist(task, slug, runner):
    result = SpecialistResult(task_id=task.task_id, agent="portfolio", status="failed")
    try:
        if set(task.constraints) - {"booth", "sections"}:
            raise ValueError("Portfolio constraints support booth and sections only")
        identifier = task.constraints.get("booth")
        if not isinstance(identifier, str) or not identifier.strip():
            result.status = "needs_clarification"
            result.missing_inputs = ["Which booth name or part number should be profiled?"]
            return result
        booth = resolve_booth(identifier, slug, runner.gcs_data)
        matches = []
        part = booth["part_number"].casefold()

        def filename_matches(filename):
            match = re.search(r"(?:part|booth)[_ -]*(\d+)(?!\d)", filename, re.I)
            exact_part = bool(match and part == f"part_{int(match[1])}")
            name = filename.removesuffix("_stats.json").removesuffix(".json").replace("_", " ").casefold()
            return exact_part or name == booth["booth_name"].casefold()

        files = runner.gcs_data.list_portfolio_files(slug)
        candidates = [filename for filename in files if filename_matches(filename)]
        # Avoid opening every portfolio when a deterministic filename identifies it.
        for filename in candidates or files:
            if execution.remaining(reserve=15) <= 0:
                raise TimeoutError("Portfolio deadline exceeded")
            portfolio = runner.gcs_data.load_portfolio_json(slug, filename)
            metadata = portfolio.get("booth_metadata") or {}
            values = {str(metadata.get(k, "")).strip().casefold() for k in ("booth_name", "part_number", "part_no")}
            explicit_parts = {
                f"part_{int(match[1])}" for value in values
                if (match := re.fullmatch(r"(?:part[ _-]*)?(\d+)", value, re.I))
            }
            # Explicit report metadata takes precedence over an attractive filename.
            if explicit_parts and explicit_parts != {part}:
                continue
            if values & {part, part.removeprefix("part_"), booth["booth_name"].casefold()} or filename_matches(filename):
                matches.append((filename, portfolio))
        if len(matches) != 1:
            raise AmbiguousInput(f"Found {len(matches)} portfolio files for {identifier}; the booth-to-file mapping needs confirmation.")
        filename, portfolio = matches[0]
        sections = task.constraints.get("sections", [])
        if not isinstance(sections, list) or any(not isinstance(s, str) for s in sections):
            raise ValueError("sections must be a list of top-level portfolio keys")
        # Bound evidence without cutting serialized JSON in the middle of a value.
        selected = {s: portfolio[s] for s in sections if s in portfolio} if sections else portfolio
        if sections:
            missing = [s for s in sections if s not in portfolio]
            if missing:
                result.limitations.append("Missing portfolio sections: " + ", ".join(missing))
        if len(dump(selected)) > 24000:
            result.status = "partial"
            result.facts = [{"resolved_booth": booth, "available_sections": list(portfolio)}]
            result.limitations.append("Portfolio is too large; request specific top-level sections listed in available_sections.")
        else:
            result.status = "partial" if result.limitations else "complete"
            result.facts = [{"resolved_booth": booth, "portfolio": selected}]
        result.sources = [{"source_file": filename, "booth_mapping": booth}]
    except AmbiguousInput as error:
        result.status = "needs_clarification"
        result.missing_inputs = [str(error)]
    except Exception as error:
        result.errors = [str(error)]
    return result


def run_specialist(task, slug, meta, runner, voter_context=None, question=""):
    if task.agent == "voter":
        # Migrate older pending sessions into the same SQL-writing boundary.
        task = task.model_copy(deep=True)
        if not task.voter_request and task.name_query:
            if task.constraints:
                raise ValueError("Legacy name requests cannot carry extra constraints")
            q = task.name_query
            task.voter_request = VoterRequest(
                operation=q.operation,
                person=q.person,
                relative=q.relative,
                booth=q.booth,
                match_mode=q.match_mode,
                filters=q.predicates
                + [VoterFilter(field=k, value=str(v)) for k, v in q.filters.items()],
                confirmed_person_variants=q.confirmed_person_variants,
                confirmed_relative_variants=q.confirmed_relative_variants,
            )
        elif not task.voter_request and task.household_query:
            task.voter_request = VoterRequest(
                operation="household", **task.household_query.model_dump()
            )
        task.name_query = task.household_query = None
        context = voter_context if voter_context is not None else {}
        try:
            task = prepare_voter_request(task, slug, runner.gcs_data, context)
        except AmbiguousInput as error:
            return SpecialistResult(
                task_id=task.task_id,
                agent="voter",
                status="needs_clarification",
                missing_inputs=[str(error)],
            )
        validate_demographic_intent(
            task.voter_request, question, voter_catalog(slug, runner.gcs_data)
        )
        result = sql_specialist(task, slug, meta, runner, question)
        remember_population(task, result, slug, runner.gcs_data, context)
        return result
    if task.agent == "research":
        validate_public_assignment(task.research_query, voter_context or {})
        return research_specialist(task, slug, meta, runner)
    if task.agent == "portfolio":
        return portfolio_specialist(task, slug, runner)
    return sql_specialist(task, slug, meta, runner)


class CoordinatorState(TypedDict, total=False):
    """Invocation state; the private session store owns durable conversation state."""

    action: CoordinatorAction
    agent_path: list[str]
    allowed_agents: list[str] | None
    batch: list
    calls: int
    catalog: list | dict | None
    constituency_id: str
    corrections: list[str]
    electoral: bool
    fingerprints: set[str]
    first_action: bool
    conversation: list[dict]
    last_error: str | None
    meta: dict
    next_node: str
    objective: str
    output: dict
    pending: dict
    pending_state: dict | None
    pending_tasks: list
    previous_clarification: str | None
    question: str
    reference_evidence: dict | None
    reference_loaded: bool
    rejection_count: int
    request_id: str
    requirements: AnswerRequirements
    research_calls: int
    results: dict
    session_id: str
    slug: str
    source_catalog: dict
    started: float
    system: str
    tasks: dict
    terminal: str
    trace: list[dict]
    turns: int
    versions: dict
    voter_context: dict
    working: dict


class Coordinator:
    """Node implementations operating on explicit LangGraph state."""

    def __init__(self, state, runner):
        self.__dict__.update(copy.deepcopy(state))
        self.runner = runner

    def prepare(self):
        self.slug = self.constituency_id.strip().lower()
        self.meta = self.runner.load_constituency_meta(self.slug)
        try:
            self.source_catalog = voter_catalog(self.slug, self.runner.gcs_data)
        except Exception:
            self.source_catalog = {
                "constituency_id": self.slug,
                "available": None,
                "reason": "Catalog could not be loaded; inspect the source before answering.",
            }
        self.system = self.runner.load_prompt("coordinator_system.txt", **self.meta)
        self.system += "\n" + self.runner.answer_style_instruction(self.question)
        self.system += (
            "\nCurrent date in Asia/Kolkata: "
            + datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        )
        self.electoral = electoral_request(self.question, self.conversation)
        self.catalog = None
        if self.electoral:
            try:
                self.catalog = election_catalog(self.slug, self.runner.gcs_data)
            except Exception:
                self.catalog = {
                    "status": "unavailable",
                    "instruction": "Ask history to inspect source availability",
                }
        self.system += "\nConstituency data coverage: " + self.meta.get(
            "data_coverage", "{}"
        )
        self.system += "\nRespect data coverage. Unavailable voter or portfolio data has not been added; never interpret missing data as zero voters. Do not delegate to unavailable specialists. For partial voter coverage, describe totals and absence claims as applying only to available records, never the entire constituency. Owner-declared coverage is not independently verified. History and public research remain independent of voter coverage."
        self.pending = self.pending_state or {}
        self.objective = self.pending.get(
            "objective", resolve_electoral_objective(self.question, self.conversation)
        )
        self.tasks = dict(self.pending.get("tasks", {}))
        self.results = dict(self.pending.get("results", {}))
        for key in ("_history_catalog", "_verified_person"):
            self.tasks.pop(key, None)
            self.results.pop(key, None)
        self.versions = {}
        for key, previous in list(self.results.items()):
            for source in previous.get("sources", []):
                filename, version = (source.get("database"), source.get("data_version"))
                if filename in {"voters.db", "history.db"} and version:
                    if filename not in self.versions:
                        try:
                            self.versions[filename] = data_version(
                                self.slug, self.runner.gcs_data, filename
                            )
                        except Exception:
                            self.versions[filename] = None
                    if self.versions[filename] != version:
                        self.results.pop(key, None)
                        break
        self.agent_path, self.corrections, self.fingerprints = ([], [], set())
        self.calls = 0
        self.research_calls = 0
        self.voter_context = copy.deepcopy(self.voter_context or {})
        self.trace = []
        self.request_id = uuid4().hex
        self.started = time.monotonic()
        self.requirements = AnswerRequirements.model_validate(
            self.pending.get(
                "answer_requirements", infer_requirements(self.objective).model_dump()
            )
        )
        self.working = copy.deepcopy(self.voter_context.pop("_working_context", {}))
        if (
            self.requirements.intent == "election_ranking"
            and self.requirements.year is None
            and self.working.get("year")
        ):
            self.requirements.year = self.working["year"]
        self.last_error = None
        self.first_action = True
        self.rejection_count = 0
        self.terminal = "turn_limit"
        self.allowed_agents = (
            source_policy(self.question)
            or self.pending.get("allowed_agents")
            or source_policy(self.objective)
        )
        self.previous_clarification = self.pending.get("clarification_question")
        self.reference_evidence, self.reference_loaded = (None, False)
        if self.allowed_agents:
            self.tasks = {
                key: value
                for key, value in self.tasks.items()
                if value.get("agent") in self.allowed_agents
            }
            self.results = {
                key: value
                for key, value in self.results.items()
                if value.get("agent") in self.allowed_agents
            }
        return "coordinator"

    def response(
        self, answer, status, persist=False, sources=None, partial_reason=None
    ):
        value = {
            "answer": self.runner.clean_user_answer(answer, self.question),
            "charts": [],
            "session_id": self.session_id,
            "agent_path": self.agent_path,
            "status": status,
        }
        value["pending_state"] = (
            {
                "objective": self.objective,
                "tasks": self.tasks,
                "results": compact_results(self.results, budget=240000),
                "answer_requirements": self.requirements.model_dump(),
                "allowed_agents": self.allowed_agents,
                "clarification_question": self.previous_clarification,
            }
            if persist
            else None
        )
        self.voter_context["_working_context"] = self.working
        value["voter_context"] = self.voter_context
        value["diagnostics"] = [
            {"request_id": self.request_id, **item}
            for item in execution.events()[-6:] + self.trace[-8:]
        ] + [
            {
                "event": "terminal",
                "request_id": self.request_id,
                "reason": self.terminal,
                "status": status,
                "elapsed_ms": int((time.monotonic() - self.started) * 1000),
            }
        ]
        value["sources"] = sources or []
        value["request_id"] = self.request_id
        value["error_code"] = self.terminal if status == "failed" else None
        value["turn_trace"] = {
            "catalog_version": CATALOG_VERSION,
            "deployment_revision": __import__("os").environ.get("K_REVISION", "local"),
            "request_id": self.request_id,
            "question": self.question[:1500],
            "objective": self.objective[:1500],
            "status": status,
            "terminal": self.terminal,
            "events": execution.events()[-24:] + self.trace[-32:],
        }
        value["partial_reason"] = (
            explain_partial(
                partial_reason,
                self.results,
                self.terminal,
                self.runner.wants_hindi(self.question),
            )
            if status == "partial"
            else None
        )
        return value

    def coordinate(self):
        if self.turns >= MAX_COORDINATOR_TURNS:
            return "fallback"
        self.turns += 1
        if execution.remaining() <= 0:
            self.terminal = "deadline_exceeded"
            return "fallback"
        if not self.reference_loaded:
            self.reference_loaded = True
            if not self.allowed_agents or "voter" in self.allowed_agents:
                try:
                    self.reference_evidence = rehydrate_selected_voter(
                        self.slug, self.runner.gcs_data, self.voter_context
                    )
                except Exception:
                    self.reference_evidence = None
                if self.reference_evidence:
                    self.trace.append(
                        {
                            "event": "verified_context_rehydrated",
                            "agent": "voter",
                            "sources": self.reference_evidence["sources"],
                        }
                    )
        if self.reference_evidence:
            self.tasks["_verified_person"] = {
                "task_id": "_verified_person",
                "agent": "voter",
                "objective": "Current attributes of the previously selected verified person only",
            }
            self.results["_verified_person"] = self.reference_evidence
        if (
            self.electoral
            and isinstance(self.catalog, list)
            and self.catalog
            and (not self.allowed_agents or "history" in self.allowed_agents)
        ):
            self.tasks["_history_catalog"] = {
                "task_id": "_history_catalog",
                "agent": "history",
                "objective": "Available election periods and row counts only",
            }
            self.results["_history_catalog"] = catalog_evidence(self.catalog)
        context = {
            "original_objective": self.objective,
            "latest_user_message": self.question,
            "conversation": (self.conversation or [])[-12:],
            "tasks": self.tasks,
            "verified_voter_context": self.voter_context,
            "source_catalog": self.source_catalog,
            "max_tasks_per_action": min(8, MAX_SPECIALIST_CALLS - self.calls),
            "working_context": self.working,
            "answer_requirements": self.requirements.model_dump(),
            "pending_clarification": bool(self.pending),
            "resolved_scope": "whole_constituency"
            if whole_constituency(self.objective + " " + self.question)
            else "resolve_from_request",
            "allowed_agents": self.allowed_agents
            or ["voter", "history", "portfolio", "research"],
            "specialist_results": compact_results(self.results),
            "corrections": self.corrections[-2:],
            "remaining_specialist_calls": MAX_SPECIALIST_CALLS - self.calls,
        }
        context["research_scope_supported"] = research_supported(self.meta)
        if self.electoral:
            context["election_catalog"] = self.catalog
            context["electoral_request"] = True
        context["remaining_research_calls"] = 2 - self.research_calls
        observations = context.pop("specialist_results")
        raw = self.runner.gemini_chat(
            [
                {"role": "system", "content": self.system},
                {
                    "role": "user",
                    "content": dump(context)
                    + "\nSpecialist observations:\n"
                    + self.runner.wrap_electoral(dump(observations)),
                },
            ],
            temperature=0.0,
            response_schema=CoordinatorAction.model_json_schema(),
        )
        if raw.startswith("<<LLM_ERROR"):
            self.terminal = failure_kind(raw)
            return "fallback"
        self.action = CoordinatorAction.model_validate(self.runner.safe_json(raw))
        return "validate"

    def validate(self):
        action = self.action
        self.trace.append(
            {
                "event": "coordinator_action",
                "action": action.action,
                "request_id": self.request_id,
            }
        )
        if self.first_action:
            if action.objective_mode == "new":
                self.objective, self.tasks, self.results = (
                    resolve_electoral_objective(self.question, self.conversation),
                    {},
                    {},
                )
                self.pending = {}
                self.requirements = infer_requirements(self.question)
                self.allowed_agents = source_policy(self.question)
                self.previous_clarification = None
            if action.answer_requirements:
                self.requirements = resolve_requirements(
                    self.objective, action.answer_requirements, self.working
                )
            self.first_action = False
        if action.action == "clarify":
            validate_clarification(
                action,
                self.requirements,
                bool(self.results),
                self.objective + " " + self.question,
                self.previous_clarification,
            )
            self.previous_clarification = action.answer
            logger.info(
                "Coordinator awaiting clarification; completed calls=%d", self.calls
            )
            self.terminal = "user_clarification"
            return self.response(action.answer, "needs_clarification", True)
        if action.action == "finish":
            validate_electoral_finish(
                action, self.electoral, self.results, self.question
            )
            if action.answer_kind == "conversation":
                if re.search(
                    "\\bhow many\\b|\\bcount\\b|कितने|कितनी|गिनती", self.question, re.I
                ) or (
                    self.conversation
                    and re.search(
                        "wrong|incorrect|mistake|category|caste|गलत|जाति|वर्ग",
                        self.question,
                        re.I,
                    )
                ):
                    raise ValueError(
                        "A count request is data, not conversation. Delegate to the appropriate specialist and cite its executed result."
                    )
                self.terminal = "conversation_completed"
                return self.response(
                    action.answer, "complete", persist=bool(self.pending)
                )
            unresolved = [
                r for r in self.results.values() if r["status"] == "needs_clarification"
            ]
            if unresolved:
                raise ValueError(
                    "Resolve missing inputs or ask the user; do not finish while specialist clarification is pending. Reuse the task_id when revising its task."
                )
            ids = action.evidence_task_ids
            if any((i not in self.results for i in ids)):
                raise ValueError(
                    "Final answer cites unknown task IDs. Current citable task IDs: "
                    + ", ".join(self.results)
                    + ". Prior-turn task IDs are not current evidence. Cite _verified_person only for that selected person's attributes; otherwise delegate a new specialist task."
                )
            if (
                ids
                and set(ids) == {"_verified_person"}
                and re.search(
                    "\\bhow many\\b|\\bcount\\b|\\btotal\\b|कितने|कितनी|कुल|गिनती",
                    self.question,
                    re.I,
                )
            ):
                raise ValueError(
                    "A selected-person lookup cannot establish a population count. Delegate a Voter count with the complete intended population and filters."
                )
            if self.allowed_agents and any(
                (self.results[i]["agent"] not in self.allowed_agents for i in ids)
            ):
                raise ValueError(
                    "Final evidence must respect the user's source restriction"
                )
            supported = [
                self.results[i]
                for i in ids
                if self.results[i]["facts"]
                and self.results[i]["status"] in ("complete", "partial")
            ]
            verified = any(
                (
                    r["status"] in {"complete", "partial"}
                    and any(
                        (
                            not f.get("no_verified_findings")
                            and (not f.get("search_leads_only"))
                            for f in r["facts"]
                        )
                    )
                    for key, r in self.results.items()
                    if (key != "_history_catalog" or coverage_request(self.question))
                    and (key != "_verified_person" or key in ids)
                )
            )
            unavailable = (
                action.status in {"failed", "partial"}
                and self.results
                and (not verified)
            )
            if not supported and (not unavailable):
                reference = self.voter_context.get("selected_reference")
                if reference:
                    raise ValueError(
                        "No current executed evidence supports this data answer. Delegate a Voter lookup with voter_request operation=lookup and reference="
                        + reference
                        + ", then cite that task. Do not repeat finish using conversation prose."
                    )
                raise ValueError(
                    "A data answer needs evidence_task_ids with successful facts. Delegate the appropriate specialist now; do not repeat finish or ask the user to fix an internal assignment."
                )
            validate_zero_claim(action.answer, supported)
            validate_count_answer(action.answer, supported)
            count_answer = render_count(
                supported, self.runner.wants_hindi(self.question)
            )
            if count_answer:
                action.answer = count_answer
            ranking = None
            if (
                self.requirements.intent == "election_ranking"
                and action.status != "failed"
            ):
                try:
                    ranking = ranking_rows(self.requirements, supported, self.tasks)
                except ValueError:
                    if action.status != "partial" or not any(
                        (r.get("gaps") for r in supported)
                    ):
                        raise
                    action.answer = (
                        "The requested ranking could not be established: "
                        + "; ".join(
                            (gap for r in supported for gap in r.get("gaps", []))
                        )
                    )
            if (
                self.requirements.intent == "public_history"
                and action.status == "complete"
            ):
                if not any(
                    (
                        r["agent"] == "research"
                        and any((f.get("text") for f in r["facts"]))
                        for r in supported
                    )
                ):
                    raise ValueError(
                        "General historical background requires public research findings, not electoral rows"
                    )
            web = [result for result in supported if result["agent"] == "research"]
            source_map = {
                source["source_id"]: source
                for result in web
                for source in result["sources"]
                if source.get("source_id") and canonical(source.get("url"))
            }
            cited = set(re.findall("\\[(R[^\\]\\s]+)\\]", action.answer))
            if cited - source_map.keys():
                raise ValueError(
                    "Final answer contains unknown web citations; use source_id values from cited research tasks"
                )
            factual_ids = {
                sid
                for result in web
                for fact in result["facts"]
                if fact.get("text")
                for sid in fact.get("source_ids", [])
            }
            if factual_ids and (not cited.intersection(factual_ids)):
                raise ValueError(
                    "Public research findings require inline [source_id] citations in the final answer"
                )
            if cited and factual_ids and cited - factual_ids:
                raise ValueError(
                    "Cite sources supporting verified research findings, not unread search leads"
                )
            final_sources = [
                source_map[sid]
                for sid in dict.fromkeys(
                    re.findall("\\[(R[^\\]\\s]+)\\]", action.answer)
                )
            ]
            if web and (not factual_ids) and (not cited):
                final_sources = list(source_map.values())[:8]
            offered = [
                item["reference"]
                for result in supported
                for fact in result["facts"]
                for item in fact.get("voter_references", [])
            ]
            if action.selected_voter_reference:
                if action.selected_voter_reference not in offered:
                    raise ValueError(
                        "Select a voter reference from the cited, verified lookup results"
                    )
                self.voter_context["selected_reference"] = (
                    action.selected_voter_reference
                )
            elif offered:
                choices = list(
                    dict.fromkeys(
                        (
                            item["reference"]
                            for result in supported
                            if not self.tasks.get(result["task_id"], {}).get(
                                "household_query"
                            )
                            for fact in result["facts"]
                            for item in fact.get("voter_references", [])
                        )
                    )
                )
                if choices:
                    self.voter_context["selected_reference"] = (
                        choices[0] if len(choices) == 1 else None
                    )
            status = action.status
            if status == "complete" and any(
                (r["status"] != "complete" or r.get("gaps") for r in supported)
            ):
                status = "partial"
            if ranking:
                action.answer = render_ranking(
                    ranking,
                    self.requirements,
                    self.meta["name"],
                    self.runner.wants_hindi(self.question),
                )
                caveats = list(
                    dict.fromkeys(
                        (
                            gap
                            for r in supported
                            for gap in r.get("gaps", []) + r.get("limitations", [])
                        )
                    )
                )
                if caveats:
                    action.answer += "\n" + "; ".join(caveats)
            if supported:
                if (
                    any((r.get("task_id") == "_verified_person" for r in supported))
                    and "voter_agent" not in self.agent_path
                ):
                    self.agent_path.append("voter_agent")
                if self.electoral and (not coverage_request(self.question)):
                    action.answer = scope_lok_answer(
                        action.answer,
                        supported,
                        self.meta["name"],
                        self.runner.wants_hindi(self.question),
                    )
                self.working["last_objective"] = self.objective[:500]
                if ranking:
                    self.working.update(
                        {
                            "year": self.requirements.year,
                            "geography": self.requirements.geography,
                        }
                    )
            verified_booths = [
                f.get("resolved_booth")
                for r in supported
                for f in r["facts"]
                if f.get("resolved_booth")
            ]
            if len(verified_booths) == 1:
                self.working["booth"] = verified_booths[0]
            self.terminal = (
                "completed"
                if status == "complete"
                else "partial_evidence"
                if status == "partial"
                else "unavailable_evidence"
            )
            return self.response(
                action.answer,
                status,
                sources=final_sources,
                partial_reason=action.partial_reason,
            )
        self.batch = action.tasks
        self.pending_tasks = list(self.batch)
        return "dispatch"

    def specialize(self):
        task = self.pending_tasks.pop(0)
        explicit_booths = set(
            re.findall(
                "\\b(?:booth|part)\\s*(?:number|no\\.?|#)?\\s*(\\d+)\\b|बूथ\\s*(\\d+)",
                self.question,
                re.I,
            )
        )
        explicit_booths = {a or b for a, b in explicit_booths}
        if (
            task.voter_request
            and task.voter_request.operation in {"count", "lookup"}
            and (len(self.batch) == 1)
            and (len(explicit_booths) == 1)
            and (
                not re.search(
                    "percentage|share|compare|versus|\\bvs\\b|प्रतिशत|तुलना",
                    self.question,
                    re.I,
                )
            )
        ):
            if not task.voter_request.booth:
                task.voter_request.booth = next(iter(explicit_booths))
        if task.booth_analysis_request:
            task.constraints = task.booth_analysis_request.model_dump()
        if task.history_request:
            task.constraints = {
                **task.constraints,
                **task.history_request.model_dump(exclude_none=True),
            }
        if (
            task.agent == "voter"
            and task.voter_request
            and re.search("\\bhow many\\b|कितने|कितनी", self.question, re.I)
        ):
            if task.voter_request.operation in {"lookup", "explore"}:
                raise ValueError(
                    "Preserve the requested count operation in voter_request; do not convert it into lookup or profile."
                )
        if task.task_id in {"_history_catalog", "_verified_person"}:
            raise ValueError(
                "This task ID is server-owned context evidence; use a new task ID for a specialist assignment"
            )
        if self.allowed_agents and task.agent not in self.allowed_agents:
            raise ValueError(
                "The user explicitly restricted the source. Delegate only to: "
                + ", ".join(self.allowed_agents)
            )
        if (
            task.agent == "portfolio"
            and (not task.constraints.get("booth"))
            and whole_constituency(self.objective + " " + self.question)
        ):
            raise ValueError(
                "Portfolio requires an individual booth identifier. For this whole-constituency request, use an aggregate source; do not ask the user to supply a booth list."
            )
        if execution.remaining(reserve=15) <= 0:
            self.terminal = "deadline_exceeded"
            raise ValueError(
                "Specialist time budget exhausted; finish with available evidence and limitations"
            )
        prepare_task(task, self.requirements)
        if self.calls >= MAX_SPECIALIST_CALLS:
            raise ValueError(
                "Specialist call limit reached; finish from supported evidence or ask for clarification"
            )
        if task.agent == "research":
            if self.research_calls >= 2:
                raise ValueError(
                    "Research call budget reached; finish with available evidence and limitations"
                )
        fingerprint = dump(task.model_dump(exclude={"task_id"}))
        if fingerprint in self.fingerprints:
            raise ValueError(
                "Identical task already executed. Inspect its result and revise the task instead of repeating it."
            )
        self.fingerprints.add(fingerprint)
        if task.agent == "research":
            self.research_calls += 1
        self.calls += 1
        self.tasks[task.task_id] = task.model_dump()
        logger.info("Delegating task=%s agent=%s", task.task_id, task.agent)
        try:
            result = run_specialist(
                task,
                self.slug,
                self.meta,
                self.runner,
                self.voter_context,
                self.question,
            )
            for source in result.sources:
                filename = source.get("database")
                if filename in {"voters.db", "history.db"}:
                    source["data_version"] = data_version(
                        self.slug, self.runner.gcs_data, filename
                    )
            remember_voters(result, self.slug, self.runner.gcs_data, self.voter_context)
        except Exception as error:
            result = SpecialistResult(
                task_id=task.task_id,
                agent=task.agent,
                status="failed",
                errors=[str(error)],
                failure_code=error.kind if isinstance(error, ModelFailure) else None,
            )
        self.results[task.task_id] = result.model_dump()
        self.trace.append(
            {
                "task_id": task.task_id,
                "agent": task.agent,
                "status": result.status,
                "operation": task.voter_request.operation
                if task.voter_request
                else "household_lookup"
                if task.household_query
                else "name_query"
                if task.name_query
                else "web_research"
                if task.research_query
                else "research",
                "filters": task.voter_request.model_dump()
                if task.voter_request
                else task.household_query.model_dump()
                if task.household_query
                else task.name_query.model_dump()
                if task.name_query
                else task.constraints,
                "errors": result.errors,
                "missing_inputs": result.missing_inputs,
                "failure_code": result.failure_code,
                "resolved_scopes": [
                    fact["count_scope"]
                    for fact in result.facts
                    if "count_scope" in fact
                ],
                "counts": [
                    fact.get("total", fact.get("row_count"))
                    for fact in result.facts
                    if "total" in fact or "row_count" in fact
                ],
                "sources": [
                    {
                        k: source[k]
                        for k in (
                            "database",
                            "data_version",
                            "source_id",
                            "source_file",
                        )
                        if k in source
                    }
                    for source in result.sources
                ],
            }
        )
        for saved, source in zip(self.trace[-1]["sources"], result.sources):
            if source.get("sql"):
                saved["sql"] = str(source["sql"])[:4000]
                saved["sql_truncated"] = len(str(source["sql"])) > 4000
            if "parameters" in source:
                saved["parameters"] = [
                    str(value)[:500] for value in source["parameters"][:32]
                ]
                saved["parameters_truncated"] = len(source["parameters"]) > 32 or any(
                    (len(str(value)) > 500 for value in source["parameters"])
                )
        logger.info("Specialist task=%s status=%s", task.task_id, result.status)
        self.agent_path.append(f"{task.agent}_agent")
        self.rejection_count, self.last_error = (0, None)
        return "dispatch"

    def reject(self, error):
        message = (
            "Invalid structured action: "
            + dump(error.errors(include_input=False, include_context=False))
            if isinstance(error, ValidationError)
            else str(error)
        )
        self.corrections.append(message[:2000])
        self.trace.append(
            {
                "event": "coordinator_rejected",
                "reason": message[:2000],
                "request_id": self.request_id,
            }
        )
        self.rejection_count = (
            self.rejection_count + 1 if message == self.last_error else 1
        )
        self.last_error = message
        if self.rejection_count >= 2:
            self.terminal = "repeated_invalid_action"
            return "fallback"
        self.pending_tasks = []
        return "coordinator"

    def fallback(self):
        self.results = {
            key: value
            for key, value in self.results.items()
            if key not in {"_verified_person", "_history_catalog"}
        }
        missing = [
            item
            for result in self.results.values()
            for item in result.get("missing_inputs", [])
        ]
        if missing:
            self.terminal = "user_clarification"
            return self.response(
                "\n".join(dict.fromkeys(missing)), "needs_clarification", True
            )
        usable = [
            r for r in self.results.values() if r["status"] in {"complete", "partial"}
        ]
        if self.requirements.intent == "election_ranking":
            try:
                rows = ranking_rows(self.requirements, usable, self.tasks)
                answer = render_ranking(
                    rows,
                    self.requirements,
                    self.meta["name"],
                    self.runner.wants_hindi(self.question),
                )
                self.working.update(
                    {
                        "year": self.requirements.year,
                        "geography": self.requirements.geography,
                    }
                )
                answer += "\n" + (
                    "यह सत्यापित आंशिक उत्तर है।"
                    if self.runner.wants_hindi(self.question)
                    else "This is a verified partial answer from the available results."
                )
                return self.response(answer, "partial")
            except ValueError:
                pass
        sources = {
            s["source_id"]: s
            for r in usable
            if r["agent"] == "research"
            for s in r["sources"]
            if s.get("source_id")
            and canonical(s.get("url"))
            and (s.get("read_status") == "page_read")
        }
        findings = [
            f
            for r in usable
            if r["agent"] == "research"
            for f in r["facts"]
            if f.get("text")
            and f.get("source_ids")
            and all((sid in sources for sid in f["source_ids"]))
        ]
        if findings:
            selected = findings[:3]
            answer = (
                "आंशिक उत्तर:\n"
                if self.runner.wants_hindi(self.question)
                else "Partial answer:\n"
            ) + "\n".join(
                (
                    f["text"] + " " + " ".join((f"[{sid}]" for sid in f["source_ids"]))
                    for f in selected
                )
            )
            answer += "\n" + (
                "पूरा ऐतिहासिक विवरण स्थापित नहीं हो पाया।"
                if self.runner.wants_hindi(self.question)
                else "I could not complete all the details requested in this attempt."
            )
            cited = list(
                dict.fromkeys((sid for f in selected for sid in f["source_ids"]))
            )
            return self.response(
                answer, "partial", sources=[sources[sid] for sid in cited]
            )
        if self.terminal in {
            "provider_capacity",
            "provider_unavailable",
            "provider_request_invalid",
        }:
            return self.response(
                user_error(self.terminal, self.runner.wants_hindi(self.question)),
                "failed",
            )
        codes = {r.get("failure_code") for r in self.results.values()}
        if self.terminal == "deadline_exceeded" or "deadline_exceeded" in codes:
            self.terminal = "deadline_exceeded"
            answer = "The request reached its time limit before a verified answer was ready. Please retry."
        elif "query_validation_failed" in codes:
            self.terminal = "query_validation_failed"
            answer = "I could not validate the generated query against the requested filters and source. No complete answer was verified."
        elif self.results and all(
            (r["status"] == "failed" for r in self.results.values())
        ):
            codes = {r.get("failure_code") for r in self.results.values()}
            if codes & {
                "provider_capacity",
                "provider_unavailable",
                "provider_request_invalid",
            }:
                self.terminal = next(
                    (
                        code
                        for code in (
                            "provider_request_invalid",
                            "provider_capacity",
                            "provider_unavailable",
                        )
                        if code in codes
                    )
                )
                return self.response(
                    user_error(self.terminal, self.runner.wants_hindi(self.question)),
                    "failed",
                )
            if "unsupported_scope" in codes:
                answer = "Public research is currently supported only for the Prayagraj and Kaushambi pilot. This constituency is outside that coverage."
                self.terminal = "unsupported_scope"
            elif "configuration_missing" in codes:
                answer = "Public research is currently unavailable because its search connection is not configured."
                self.terminal = "configuration_missing"
            elif "data_unavailable" in codes:
                answer = "The records needed for this request are currently unavailable for the selected constituency."
                self.terminal = "data_unavailable"
            else:
                answer = "The analysis attempts ended without verified query results. Please retry; the request ID can be used to investigate this failure."
        elif self.results:
            answer = ("The generated answer repeatedly failed verification against the available evidence."
                      if self.terminal == "repeated_invalid_action" else
                      "The analysis reached its step limit before a complete answer could be verified.")
        else:
            answer = ("The AI service repeatedly generated an invalid action and could not complete this request."
                      if self.terminal == "repeated_invalid_action" else
                      "The analysis reached its step limit without producing verified results.")
        if self.runner.wants_hindi(self.question):
            answer = {
                "query_validation_failed": "तैयार की गई क्वेरी का अनुरोधित फ़िल्टर और स्रोत से मिलान सत्यापित नहीं हो पाया। पूरा उत्तर सत्यापित नहीं हुआ।",
                "deadline_exceeded": "सत्यापित उत्तर तैयार होने से पहले समय सीमा समाप्त हो गई। कृपया दोबारा प्रयास करें।",
                "repeated_invalid_action": "तैयार किए गए उत्तर या कार्रवाई का बार-बार सत्यापन विफल हुआ।",
                "turn_limit": "पूरा उत्तर सत्यापित होने से पहले विश्लेषण की चरण सीमा समाप्त हो गई।",
                "data_unavailable": "चुने गए निर्वाचन क्षेत्र के लिए आवश्यक रिकॉर्ड अभी उपलब्ध नहीं हैं।",
            }.get(self.terminal, "इस प्रयास में आपके प्रश्न का सत्यापित उत्तर पूरा नहीं हो पाया। कृपया दोबारा प्रयास करें।")
        answer += "\n" + ("अनुरोध ID: " if self.runner.wants_hindi(self.question) else "Request ID: ") + self.request_id
        return self.response(
            answer,
            "partial"
            if any(
                (
                    r["facts"] and r["status"] in {"complete", "partial"}
                    for r in self.results.values()
                )
            )
            else "failed",
        )


@lru_cache(maxsize=4)
def build_graph(runner):
    """Compile separate planning, validation, specialist and recovery nodes.

    Cross-turn persistence and leases remain in the user-scoped session store.
    No external checkpointer or tracing service is enabled here.
    """
    from langgraph.graph import END, START, StateGraph

    def node(method):
        def run(state):
            context = Coordinator(state, runner)
            try:
                result = getattr(context, method)()
            except (ValidationError, ValueError) as error:
                if method in {"prepare", "fallback"}:
                    raise
                result = context.reject(error)
            if isinstance(result, dict):
                context.output, context.next_node = result, END
            else:
                context.next_node = result
            return {
                key: value for key, value in vars(context).items() if key != "runner"
            }

        return run

    def dispatch(state):
        pending = state.get("pending_tasks", [])
        return {"next_node": pending[0].agent if pending else "coordinator"}

    graph = StateGraph(CoordinatorState)
    graph.add_node("prepare", node("prepare"))
    graph.add_node("coordinator", node("coordinate"))
    graph.add_node("validate", node("validate"))
    graph.add_node("dispatch", dispatch)
    for name in ("voter", "history", "portfolio", "research"):
        graph.add_node(name, node("specialize"))
    graph.add_node("fallback", node("fallback"))
    graph.add_edge(START, "prepare")
    routes = {
        "prepare": ("coordinator",),
        "coordinator": ("validate", "coordinator", "fallback"),
        "validate": ("dispatch", "coordinator", "fallback", END),
        "dispatch": ("voter", "history", "portfolio", "research", "coordinator"),
        **{
            name: ("dispatch", "coordinator", "fallback")
            for name in ("voter", "history", "portfolio", "research")
        },
        "fallback": (END,),
    }
    for name, targets in routes.items():
        graph.add_conditional_edges(
            name,
            lambda state: state["next_node"],
            {target: target for target in targets},
        )
    return graph.compile()


def run_coordinator(
    question,
    constituency_id,
    session_id,
    history,
    pending_state,
    runner,
    voter_context=None,
):
    """Invoke the graph using the existing API/session contract."""
    with tracing_context(enabled=False):
        state = build_graph(runner).invoke(
            {
                "question": question,
                "constituency_id": constituency_id,
                "session_id": session_id,
                "conversation": history or [],
                "pending_state": pending_state,
                "voter_context": voter_context or {},
                "turns": 0,
                "pending_tasks": [],
            },
            config={"recursion_limit": 128},
        )
    return state["output"]
