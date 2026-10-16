"""Bounded coordinator ReAct loop and evidence-producing specialists."""

import json
import copy
import logging
import re
from contextlib import closing

from pydantic import ValidationError

from agents.contracts import CoordinatorAction, ResearchAction, SpecialistResult
from agents.name_matching import resolve_booth, run_name_task, AmbiguousInput
from agents.households import household_lookup, remember_voters, name_predicate, data_version
from agents.research import research_specialist, supported as research_supported, canonical

MAX_COORDINATOR_TURNS = 8
MAX_SPECIALIST_CALLS = 8
logger = logging.getLogger(__name__)


def dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def sql_specialist(task, slug, meta, runner):
    """Return actual query observations, never model-invented facts."""
    filename = "voters.db" if task.agent == "voter" else "history.db"
    result = SpecialistResult(task_id=task.task_id, agent=task.agent, status="failed")
    if task.agent == "voter" and task.name_query:
        return run_name_task(task, slug, runner.gcs_data, runner.gemini_chat, runner.safe_json)
    try:
        with closing(runner.gcs_data.open_sqlite(slug, filename)) as conn:
            schema = []
            for table in runner.gcs_data.list_tables(conn):
                columns = runner.gcs_data.list_columns(conn, table)
                samples = {c: runner.gcs_data.sample_values(conn, table, c, 20)
                           for c in ("region", "party", "area", "gender", "caste", "religion") if c in columns}
                schema.append({"table": table, "columns": columns, "samples": samples})
            system = runner.load_prompt(f"{task.agent}_agent_system.txt", **meta)
            system += "\nResponse schema:\n" + dump(ResearchAction.model_json_schema())
            memory = []
            for _ in range(5 if task.agent == "voter" else 6):
                raw = runner.gemini_chat([
                    {"role": "system", "content": system},
                    {"role": "user", "content": dump({"task": task.model_dump()}) + "\n" +
                     runner.wrap_electoral(dump({"schema": schema, "observations": memory}))},
                ], temperature=0.0)
                if raw.startswith("<<LLM_ERROR"):
                    result.errors.append("The specialist model is unavailable.")
                    break
                try:
                    action = ResearchAction.model_validate(runner.safe_json(raw))
                    if action.action == "clarify":
                        result.status = "needs_clarification"
                        result.missing_inputs = action.missing_inputs
                        result.limitations.extend(action.limitations)
                        return result
                    if action.action == "finish":
                        result.limitations.extend(action.limitations)
                        result.status = action.status if result.facts else "failed"
                        if not result.facts:
                            result.errors.append("Specialist finished without successful evidence.")
                        return result
                    if task.agent == "voter" and name_predicate(action.sql):
                        raise ValueError("Name predicates require name_query, not ad hoc name SQL. Ask the coordinator for a name_query task.")
                    observation = runner.gcs_data.execute_select(conn, action.sql)
                    source_id = f"{task.task_id}:q{len(result.sources) + 1}"
                    result.sources.append({"source_id": source_id, "database": filename, "sql": action.sql})
                    result.facts.append({"source_id": source_id, **observation})
                    memory.append({"sql": action.sql, "result": observation})
                    if observation["truncated"]:
                        result.limitations.append(f"{source_id}: rows truncated to 80; use aggregation for totals.")
                except Exception as error:
                    # Corrections consume the same bounded specialist budget.
                    memory.append({"error": str(error)})
            result.status = "partial" if result.facts else "failed"
            result.errors.extend(item["error"] for item in memory[-3:] if "error" in item)
            result.limitations.append("Specialist turn limit reached or model unavailable before completion.")
            if not result.facts:
                result.errors.append("No successful query evidence; inspect task constraints or schema.")
    except Exception as error:
        result.errors.append(str(error))
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


def run_specialist(task, slug, meta, runner, voter_context=None):
    if task.agent == "research":
        return research_specialist(task, slug, meta, runner)
    if task.household_query:
        return household_lookup(task, slug, runner.gcs_data, voter_context or {})
    if task.agent == "portfolio":
        return portfolio_specialist(task, slug, runner)
    return sql_specialist(task, slug, meta, runner)


def run_coordinator(question, constituency_id, session_id, history, pending_state, runner, voter_context=None):
    slug = constituency_id.strip().lower()
    meta = runner.load_constituency_meta(slug)
    system = runner.load_prompt("coordinator_system.txt", **meta)
    system += "\nValidated response schema:\n" + dump(CoordinatorAction.model_json_schema())
    system += "\n" + runner.answer_style_instruction(question)
    # Only server-owned pending state is accepted here, never client task state.
    pending = pending_state or {}
    objective = pending.get("objective", question)
    tasks = dict(pending.get("tasks", {}))
    results = dict(pending.get("results", {}))
    agent_path, corrections, fingerprints = [], [], set()
    calls = 0
    research_calls = 0
    voter_context = copy.deepcopy(voter_context or {})
    trace = []

    def response(answer, status, persist=False, sources=None):
        value = {"answer": runner.clean_user_answer(answer, question), "charts": [],
                 "session_id": session_id, "agent_path": agent_path, "status": status}
        value["pending_state"] = {"objective": objective, "tasks": tasks, "results": results} if persist else None
        value["voter_context"] = voter_context
        value["diagnostics"] = trace[-8:]
        value["sources"] = sources or []
        return value

    for _ in range(MAX_COORDINATOR_TURNS):
        context = {"original_objective": objective, "latest_user_message": question,
                   "conversation": (history or [])[-12:], "tasks": tasks,
                   "verified_voter_context": voter_context,
                   "specialist_results": results, "corrections": corrections,
                   "remaining_specialist_calls": MAX_SPECIALIST_CALLS - calls}
        context["research_scope_supported"] = research_supported(meta)
        context["remaining_research_calls"] = 2 - research_calls
        observations = context.pop("specialist_results")
        raw = runner.gemini_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": dump(context) + "\nSpecialist observations:\n" + runner.wrap_electoral(dump(observations))},
        ], temperature=0.0)
        if raw.startswith("<<LLM_ERROR"):
            return response("The AI service is temporarily unavailable. Please try again.", "failed", bool(pending))
        try:
            action = CoordinatorAction.model_validate(runner.safe_json(raw))
            if action.action == "clarify":
                logger.info("Coordinator awaiting clarification; completed calls=%d", calls)
                return response(action.answer, "needs_clarification", True)
            if action.action == "finish":
                unresolved = [r for r in results.values() if r["status"] == "needs_clarification"]
                if unresolved:
                    raise ValueError("Resolve missing inputs or ask the user; do not finish while specialist clarification is pending. Reuse the task_id when revising its task.")
                ids = action.evidence_task_ids
                if any(i not in results for i in ids):
                    raise ValueError("Final answer cites unknown task IDs")
                supported = [results[i] for i in ids if results[i]["facts"] and results[i]["status"] in ("complete", "partial")]
                greeting = re.fullmatch(r"\s*(?:hi|hello|hey|नमस्ते|नमस्कार|what can you do\??|who are you\??|help)\s*[!.?]*\s*", question, re.I)
                unavailable = action.status == "failed" and results and all(r["status"] == "failed" for r in results.values())
                if not supported and not greeting and not unavailable:
                    raise ValueError("A data answer needs evidence_task_ids with successful facts. If evidence is unavailable, clarify what is missing.")
                web = [result for result in supported if result["agent"] == "research"]
                source_map = {source["source_id"]: source for result in web for source in result["sources"]
                              if source.get("source_id") and canonical(source.get("url"))}
                cited = set(re.findall(r"\[(R[^\]\s]+)\]", action.answer))
                if cited - source_map.keys():
                    raise ValueError("Final answer contains unknown web citations; use source_id values from cited research tasks")
                factual_ids = {sid for result in web for fact in result["facts"] if fact.get("text") for sid in fact.get("source_ids", [])}
                if factual_ids and not cited.intersection(factual_ids):
                    raise ValueError("Public research findings require inline [source_id] citations in the final answer")
                if cited and factual_ids and cited - factual_ids:
                    raise ValueError("Cite sources supporting verified research findings, not unread search leads")
                final_sources = [source_map[sid] for sid in dict.fromkeys(re.findall(r"\[(R[^\]\s]+)\]", action.answer))]
                if web and not factual_ids and not cited:
                    final_sources = list(source_map.values())[:8]
                offered = [item["reference"] for result in supported for fact in result["facts"]
                           for item in fact.get("voter_references", [])]
                if action.selected_voter_reference:
                    if action.selected_voter_reference not in offered:
                        raise ValueError("Select a voter reference from the cited, verified lookup results")
                    voter_context["selected_reference"] = action.selected_voter_reference
                elif offered:
                    choices = list(dict.fromkeys(
                        item["reference"] for result in supported
                        if not tasks.get(result["task_id"], {}).get("household_query")
                        for fact in result["facts"] for item in fact.get("voter_references", [])
                    ))
                    if choices:
                        voter_context["selected_reference"] = choices[0] if len(choices) == 1 else None
                status = action.status
                if status == "complete" and any(r["status"] != "complete" for r in results.values()):
                    status = "partial"
                return response(action.answer, status, sources=final_sources)
            for task in action.tasks:
                if calls >= MAX_SPECIALIST_CALLS:
                    raise ValueError("Specialist call limit reached; finish from supported evidence or ask for clarification")
                if task.agent == "research":
                    if research_calls >= 2:
                        raise ValueError("Research call budget reached; finish with available evidence and limitations")
                    research_calls += 1
                fingerprint = dump(task.model_dump(exclude={"task_id"}))
                if fingerprint in fingerprints:
                    raise ValueError("Identical task already executed. Inspect its result and revise the task instead of repeating it.")
                # No raw-question fallback: every call must have its own validated assignment.
                fingerprints.add(fingerprint)
                calls += 1
                tasks[task.task_id] = task.model_dump()
                logger.info("Delegating task=%s agent=%s", task.task_id, task.agent)
                try:
                    result = run_specialist(task, slug, meta, runner, voter_context)
                    for source in result.sources:
                        filename = source.get("database")
                        if filename in {"voters.db", "history.db"}:
                            source["data_version"] = data_version(slug, runner.gcs_data, filename)
                    remember_voters(result, slug, runner.gcs_data, voter_context)
                except Exception as error:
                    result = SpecialistResult(task_id=task.task_id, agent=task.agent, status="failed", errors=[str(error)])
                results[task.task_id] = result.model_dump()
                trace.append({"task_id": task.task_id, "agent": task.agent, "status": result.status,
                              "operation": "household_lookup" if task.household_query else "name_query" if task.name_query else "web_research" if task.research_query else "research",
                              "filters": task.household_query.model_dump() if task.household_query else task.name_query.model_dump() if task.name_query else task.constraints,
                              "counts": [fact.get("total", fact.get("row_count")) for fact in result.facts
                                         if "total" in fact or "row_count" in fact],
                              "sources": [{k: source[k] for k in ("database", "data_version", "source_id", "source_file") if k in source}
                                          for source in result.sources]})
                # Store bounded query diagnostics privately, never in logs or the API response.
                for saved, source in zip(trace[-1]["sources"], result.sources):
                    if source.get("sql"):
                        saved["sql"] = str(source["sql"])[:4000]
                    if "parameters" in source:
                        saved["parameters"] = [str(value)[:500] for value in source["parameters"][:32]]
                logger.info("Specialist task=%s status=%s", task.task_id, result.status)
                agent_path.append(f"{task.agent}_agent")
        except (ValidationError, ValueError) as error:
            corrections.append(str(error))
    missing = [item for result in results.values() for item in result.get("missing_inputs", [])]
    if missing:
        return response("\n".join(dict.fromkeys(missing)), "needs_clarification", True)
    answer = ("मैं इस प्रयास में विश्वसनीय उत्तर पूरा नहीं कर पाया। कृपया प्रश्न को अधिक विशिष्ट करें।"
              if runner.wants_hindi(question) else
              "I could not complete a reliable answer within this attempt. Please make the question more specific.")
    return response(answer, "partial" if any(r["facts"] for r in results.values()) else "failed")
