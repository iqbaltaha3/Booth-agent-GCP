"""Render a scalar count from executed scope, never a model's replacement label."""
from agents.contracts import CountEvidence


def render_count(supported, hindi=False):
    facts = [f for r in supported for f in r.get("facts", []) if f.get("validated_count") and f.get("count_scope")]
    # Multiple requested outputs stay with the coordinator; don't discard work.
    if len(facts) != 1 or len(supported) != 1:
        return None
    scope = CountEvidence.model_validate(facts[0]["count_scope"])
    if scope.total != facts[0]["total"]:
        raise ValueError("Count scope and executed total differ")
    names = {"social_category": "social category", "polling_station": "polling station", "house_no": "house number", "relation_type": "relationship type", "epic_id": "voter ID"}
    operators = {"eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "contains": "contains", "is_null": "is missing", "in": "in", "not_in": "not in"}
    def describe(f):
        values = f.resolved_values or f.values or ([f.value] if f.operator != "is_null" else [])
        return f'{names.get(f.field, f.field)} {operators[f.operator]} ' + " / ".join(str(v) for v in values)
    conditions = [describe(f).strip() for f in scope.filters]
    if scope.any_of:
        conditions.append("(" + " OR ".join(describe(f).strip() for f in scope.any_of) + ")")
    if scope.person:
        conditions.append(f"name ({scope.match_mode}) = {scope.person}")
    if scope.relative:
        conditions.append(f"relative name ({scope.match_mode}) = {scope.relative}")
    if hindi:
        answer = f"उपलब्ध डेटा में {scope.total:,} मिलते हुए मतदाता रिकॉर्ड हैं।"
        if conditions:
            answer += "\nदर्ज विवरण: " + "; ".join(conditions) + "।"
    else:
        answer = ("There is 1 matching voter record in the available data." if scope.total == 1
                  else f"There are {scope.total:,} matching voter records in the available data.")
        if conditions:
            answer += "\nRecorded filters: " + "; ".join(conditions) + "."
    return answer
