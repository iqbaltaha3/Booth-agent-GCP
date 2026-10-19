"""Offline specialist double: emits SQL actions, never fabricated result rows.

Expected counts remain asserted separately against fictional fixtures. This is
not an evaluation of a real model's SQL design ability.
"""
import json


def specialist_reply(messages):
    content = messages[1]['content']
    task, _ = json.JSONDecoder().raw_decode(content)
    task = task['task']
    payload = json.loads(content.split('<electoral_data>', 1)[1].split('</electoral_data>', 1)[0])
    observations = payload['observations']
    request = task.get('voter_request') or {}
    predicate = (payload.get('resolved_population') or {}).get('mandatory_base_predicate', '1=1')
    if not observations:
        return json.dumps({'action':'sql', 'sql':f'SELECT COUNT(*) AS total FROM voters WHERE {predicate}'})
    if len(observations) == 1 and request.get('operation') in {'lookup', 'household'}:
        return json.dumps({'action':'sql', 'sql':f'SELECT * FROM voters WHERE {predicate} ORDER BY epic_id LIMIT 20'})
    return json.dumps({'action':'finish', 'status':'complete'})


def with_specialist(coordinator_replies):
    replies = iter(coordinator_replies)
    def chat(messages, **kwargs):
        if kwargs.get('response_schema', {}).get('title') == 'ResearchAction':
            return specialist_reply(messages)
        return next(replies)
    return chat
