"""Offline specialist double: emits SQL actions, never fabricated result rows.

Expected counts remain asserted separately against fictional fixtures. This is
not an evaluation of a real model's SQL design ability.
"""

import json


def specialist_reply(messages):
    content = messages[1]["content"]
    task, _ = json.JSONDecoder().raw_decode(content)
    task = task["task"]
    payload = json.loads(
        content.split("<electoral_data>", 1)[1].split("</electoral_data>", 1)[0]
    )
    observations = payload["observations"]
    request = task.get("voter_request") or {}
    predicate = (payload.get("resolved_population") or {}).get(
        "suggested_population_predicate", "1=1"
    )
    if not observations:
        return json.dumps(
            {
                "action": "sql",
                "sql": f"SELECT COUNT(*) AS total FROM voters WHERE {predicate}",
            }
        )
    if len(observations) == 1 and request.get("operation") in {"lookup", "household"}:
        return json.dumps(
            {
                "action": "sql",
                "sql": f"SELECT * FROM voters WHERE {predicate} ORDER BY name",
            }
        )
    return json.dumps({"action": "finish", "status": "complete"})


def with_specialist(coordinator_replies):
    replies = iter(coordinator_replies)
    def chat(messages, **kwargs):
        if kwargs.get('response_schema', {}).get('title') == 'ResearchAction':
            return specialist_reply(messages)
        return next(replies)
    return chat


def run_voter_task(task, slug, runner, context=None, variants=None):
    """Exercise the currently routed specialist; independently asserted fixtures grade results."""
    from unittest.mock import patch
    from agents.runtime import run_specialist
    from agents.contracts import SpecialistResult

    def respond(messages, **kwargs):
        if kwargs.get("response_schema", {}).get("title") == "NameCandidates":
            return json.dumps({"variants": variants or ["श्याम"]})
        return specialist_reply(messages)

    with patch.object(runner, "gemini_chat", side_effect=respond) as model:
        try:
            result = run_specialist(task, slug, {}, runner, context or {})
        except Exception as error:
            result = SpecialistResult(
                task_id=task.task_id,
                agent="voter",
                status="failed",
                errors=[str(error)],
            )
    return result, model
