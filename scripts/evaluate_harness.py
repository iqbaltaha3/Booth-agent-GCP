"""Opt-in live routing evaluation; complements full-loop synthetic regression tests.

Run from the repository: PYTHONPATH=backend:. python scripts/evaluate_harness.py
  --live --trials 3 [--case history_constituency --case top_regions]

Uses configured Gemini/ADC, makes billed model requests, reads no voter records,
and writes only routing outcomes/metadata. This does not grade final factual answers.
"""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from agents import execution, local_runner as runner
from agents.completion import infer_requirements, resolve_requirements, prepare_task, validate_clarification
from agents.contracts import CoordinatorAction


CASES = [
    ("history_constituency", "Tell me about the history of this constituency", "research", "public_history"),
    ("history_south", "Tell me about the history of Allahabad South", "research", "public_history"),
    ("history_hindi", "इस विधानसभा क्षेत्र का इतिहास बताइए", "research", "public_history"),
    ("history_mixed", "Allahabad South ka itihaas batao", "research", "public_history"),
    ("top_regions", "Tell me about the top regions where BJP was ahead in 2024", "history", "election_ranking"),
    ("top_five", "Top 5 regions where BJP led in 2024", "history", "election_ranking"),
    ("top_booths", "Top 10 booths where BJP led in 2024", "history", "election_ranking"),
    ("top_share", "Top regions for BJP by vote share in 2024", "history", "election_ranking"),
    ("top_hindi", "2024 में भाजपा सबसे आगे किन क्षेत्रों में थी?", "history", "election_ranking"),
    ("election_history", "How did BJP perform in past elections?", "history", "electoral_history"),
    ("winner_2024", "Who won the 2024 election in this assembly segment?", "history", "general"),
    ("party_total", "How many votes did BJP receive here in 2024?", "history", "general"),
    ("turnout", "Compare turnout in 2019 and 2024", "history", "general"),
    ("news", "What are the latest public civic announcements here?", "research", "general"),
    ("old_public", "What official historical records describe this constituency?", "research", "public_history"),
    ("voter_total", "How many voters are in this constituency?", "voter", "general"),
    ("name_count", "How many people named Shyam are in booth 2?", "voter", "general"),
    ("name_hindi", "बूथ 2 में श्याम नाम के कितने मतदाता हैं?", "voter", "general"),
    ("booth_profile", "Show me the portfolio for booth 2", "portfolio", "general"),
    ("house_followup", "Find people living in the same house", "voter", "general"),
    ("religion_count", "How many Muslim voters are there?", "voter", "general"),
    ("religion_followup", "How many of these are Muslims?", "voter", "general"),
    ("gender_followup", "How many women among them?", "voter", "general"),
    ("reset_total", "Total voters in the constituency", "voter", "general"),
    ("religion_hindi", "इस क्षेत्र में कितने मुस्लिम मतदाता हैं?", "voter", "general"),
    ("religion_hinglish", "Yahan kitne Muslim voters hain?", "voter", "general"),
    ("female_count", "How many female voters?", "voter", "general"),
    ("age_count", "How many voters are over 60?", "voter", "general"),
    ("combined_count", "How many Muslim women aged over 60?", "voter", "general"),
    ("caste_count", "Count Rajput voters", "voter", "general"),
    ("booth_age_count", "How many voters over 60 in booth 2?", "voter", "general"),
    ("booth_address", "Find Shyam's house number in booth 2", "voter", "general"),
    ("booth_names", "List voter names in booth 2", "voter", "general"),
    ("booth_epic", "Find voter details for EPIC TEST0001 in booth 2", "voter", "general"),
    ("booth_demographic_profile", "Explain the prepared demographic profile of booth 2", "portfolio", "general"),
    ("booth_report_gender", "Explain the caste-gender table in booth 2's prepared report", "portfolio", "general"),
    ("available_years", "Which election years have result records?", "history", "general"),
    ("assembly_winner", "Who won the Assembly election in 2022?", "history", "general"),
    ("election_compare", "Compare candidate vote totals for the last two elections", "history", "general"),
    ("historical_background", "Explain the historical background of this town", "research", "general"),
    ("current_officeholder", "Who is the current MP?", "research", "general"),
    ("recent_news_hindi", "यहाँ की आज की खबरें बताइए", "research", "general"),
    ("public_notice", "Find the latest official water supply notice", "research", "general"),
    ("voter_grouping", "Group voter records by religion and gender", "voter", "general"),
    ("voter_age_ranking", "Which booths have the largest number of voters aged over 60?", "voter", "general"),
]


def trial(case):
    identifier, question, expected_agent, expected_intent = case
    meta = {"name": "Test Seat", "seat_type": "Vidhan Sabha", "district": "Prayagraj", "state": "Uttar Pradesh", "product_name": "Arjun"}
    context = {"original_objective": question, "latest_user_message": question,
               "conversation": [], "tasks": {}, "corrections": [], "working_context": {},
               "pending_clarification": False, "remaining_specialist_calls": 8,
               "remaining_research_calls": 2, "research_scope_supported": True,
               "answer_requirements": infer_requirements(question).model_dump(),
               "verified_voter_context": {}}
    if identifier == "house_followup":
        context["verified_voter_context"] = {"selected_reference": "synthetic-reference",
            "references": {"synthetic-reference": {"name": "Synthetic person"}}}
    if identifier in {"religion_followup", "gender_followup", "reset_total"}:
        context["verified_voter_context"] = {"population": {"filters": [] if identifier == "religion_followup" else [{"field": "religion", "operator": "eq", "value": "Muslim"}], "constituency_id": "synthetic-seat"}}
        context["conversation"] = [{"role": "user", "content": "How many voters?" if identifier == "religion_followup" else "How many Muslim voters?"}]
    if expected_agent == "history" and identifier != "available_years":
        context["election_catalog"] = [{'year': year, 'election_type': kind, 'row_count': 0 if year == 2017 else 2}
            for year, kind in [(2017,'vidhan'),(2019,'lok'),(2022,'vidhan'),(2024,'lok')]]
    with execution.execution_scope(60):
        messages = [
            {"role": "system", "content": runner.load_prompt("coordinator_system.txt", **meta)},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False) +
             "\nSpecialist observations:\n" + runner.wrap_electoral("{}")},
        ]
        repairs = 0
        try:
            for attempt in range(2):
                raw = runner.gemini_chat(messages, temperature=0, response_schema=CoordinatorAction.model_json_schema())
                try:
                    action = CoordinatorAction.model_validate(runner.safe_json(raw))
                    validate_clarification(action, resolve_requirements(question, action.answer_requirements), False)
                    for assignment in action.tasks:
                        prepare_task(assignment, resolve_requirements(question, action.answer_requirements))
                    break
                except ValueError as error:
                    if attempt or raw.startswith("<<LLM_ERROR"):
                        raise
                    details = error.errors(include_input=False, include_context=False) if hasattr(error, "errors") else str(error)
                    messages.append({"role": "user", "content": "Repair the structured action. " + json.dumps(details)})
                    repairs += 1
            requirements = resolve_requirements(question, action.answer_requirements)
            adjusted = requirements != action.answer_requirements
            for assignment in action.tasks:
                prepare_task(assignment, requirements)
            matched = [task for task in action.tasks if task.agent == expected_agent]
            passed = action.action == "delegate" and bool(matched)
            if expected_intent in {"public_history", "electoral_history", "election_ranking"}:
                passed &= requirements.intent == expected_intent
            if expected_intent == "public_history":
                passed &= any(t.research_query and (t.research_query.time_mode in {"historical", "general"} or not t.research_query.time_sensitive) for t in matched)
            if expected_intent == "election_ranking":
                req = requirements
                expected_geo = "booth" if identifier == "top_booths" else "region"
                expected_metric = "vote_share" if identifier == "top_share" else "margin"
                passed &= bool(req and req.year == 2024 and req.geography == expected_geo and req.metric == expected_metric)
            if identifier == "house_followup":
                passed &= any((t.household_query and t.household_query.reference == "synthetic-reference") or
                    (t.voter_request and t.voter_request.operation == "household" and t.voter_request.reference == "synthetic-reference") for t in matched)
            if expected_agent == "voter":
                passed &= any(t.voter_request is not None for t in matched)
            if identifier in {"religion_count", "religion_followup", "gender_followup", "reset_total", "religion_hindi", "religion_hinglish", "female_count", "age_count", "combined_count", "caste_count", "booth_age_count", "voter_total"}:
                passed &= any(t.voter_request and t.voter_request.operation == "count" and not t.voter_request.person and not t.voter_request.relative for t in matched)
            if identifier == "gender_followup":
                passed &= any(t.voter_request and (t.voter_request.population_mode == "refine" or
                    any(f.field == "religion" and f.value.casefold() in {"muslim", "muslims"} for f in t.voter_request.filters)) for t in matched)
            if identifier == "reset_total":
                passed &= any(t.voter_request and t.voter_request.population_mode == "new" and not t.voter_request.filters for t in matched)
            return {"case": identifier, "passed": bool(passed), "action": action.action,
                    "agents": [task.agent for task in action.tasks], "contract_adjusted": adjusted,
                    "requirements": requirements.model_dump(), "tasks": [t.model_dump() for t in action.tasks],
                    "answer": action.answer,
                    "validation_repairs": repairs,
                    "metadata": execution.events()}
        except ValueError as error:
            details = error.errors(include_input=False, include_context=False) if hasattr(error, "errors") else str(error)
            return {"case": identifier, "passed": False, "error": "invalid_model_action", "details": details,
                    "provider_error": raw if raw.startswith("<<LLM_ERROR") else None, "metadata": execution.events()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize billed requests through configured Gemini credentials")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--case", action="append", choices=[c[0] for c in CASES])
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to run model requests; use backend/tests for offline regression checks")
    if not 1 <= args.trials <= 10:
        parser.error("--trials must be between 1 and 10")
    if not 1 <= args.workers <= 3:
        parser.error("--workers must be between 1 and 3")
    jobs = [(case, index + 1) for case in CASES if not args.case or case[0] in args.case
            for index in range(args.trials)]
    def run(job):
        case, index = job
        return {"trial": index, **trial(case)}
    outcomes = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(run, jobs):
            outcomes.append(result)
            print(json.dumps(result), flush=True)
    print(json.dumps({"evaluation": "routing_only", "passed": sum(r["passed"] for r in outcomes), "total": len(outcomes)}))
    return 0 if all(r["passed"] for r in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
