"""Per-request deadlines and metadata, isolated across concurrent conversations."""

import time
from contextlib import contextmanager
from contextvars import ContextVar

_state = ContextVar("agent_execution", default=None)


@contextmanager
def execution_scope(seconds=240):
    state = {"deadline": time.monotonic() + seconds, "events": []}
    token = _state.set(state)
    try:
        yield state
    finally:
        _state.reset(token)


def deadline(requested=None, reserve=0):
    state = _state.get()
    shared = state["deadline"] - reserve if state else None
    values = [value for value in (requested, shared) if value is not None]
    return min(values) if values else None


def remaining(reserve=0):
    end = deadline(reserve=reserve)
    return max(0, end - time.monotonic()) if end else float("inf")


def record(event):
    state = _state.get()
    if state is not None:
        state["events"] = (state["events"] + [event])[-24:]


def events():
    state = _state.get()
    return list(state["events"]) if state else []


def provider_schema(schema):
    """Inline local refs and retain Gemini's supported structural fields only.

    Business rules (extra keys, max lengths, cross-field rules) stay in Pydantic.
    Free-form dictionaries intentionally remain objects without invented keys.
    """
    definitions = schema.get("$defs", {})
    # Nested array bounds make the coordinator schema exceed Vertex's grammar
    # state budget. Bounds/format/business rules are enforced after generation.
    supported = {"type", "description", "enum", "format", "nullable", "required",
                 "properties", "items", "anyOf"}

    def convert(node):
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            node = {**definitions[node["$ref"].split("/")[-1]],
                    **{k: v for k, v in node.items() if k != "$ref"}}
        result = {}
        for key, value in node.items():
            if key not in supported:
                continue
            if key == "properties":
                result[key] = {name: convert(child) for name, child in value.items()}
                # Gemini needs declared object properties. These dictionaries are
                # model-authored tool arguments, not arbitrary observed data.
                for name, fields in {
                    "constraints": {"year": {"type": "integer"}, "booth": {"type": "string"},
                                    "sections": {"type": "array", "items": {"type": "string"}},
                                    "party": {"type": "string"}, "area": {"type": "string"}},
                    "filters": {column: {"type": "string"} for column in
                                ("house_no", "polling_station", "gender", "region", "age", "caste", "religion")},
                }.items():
                    if name in result[key] and result[key][name].get("type") == "object" and not result[key][name].get("properties"):
                        result[key][name] = {"type": "object", "properties": fields}
            elif key == "anyOf":
                non_null = [child for child in value if child.get("type") != "null"]
                if len(non_null) != len(value):
                    result["nullable"] = True
                if len(non_null) == 1:
                    result.update(convert(non_null[0]))
                else:
                    result[key] = [convert(child) for child in non_null]
            elif key == "items":
                result[key] = convert(value)
            else:
                result[key] = value
        if "const" in node:
            result["enum"] = [node["const"]]
        # All array bounds remain in Pydantic. Combining nested domain payloads
        # with provider maxItems multiplies Vertex grammar states excessively.
        properties = result.get("properties", {})
        if "objective_mode" in properties and "answer_requirements" in properties:
            result["required"] = list(dict.fromkeys(result.get("required", []) + ["answer_kind", "objective_mode"]))
            # The coordinator owns the answer contract; runtime attaches it to
            # specialist tasks. Duplicating it in every task bloats the grammar.
            task_properties = properties.get("tasks", {}).get("items", {}).get("properties", {})
            task_properties.pop("answer_requirements", None)
            # These are internal helper contracts, not routing choices exposed
            # to the conversation orchestrator. Legacy sessions still parse.
            for internal in ("name_query", "household_query", "constraints"):
                task_properties.pop(internal, None)
            task_schema = properties.get("tasks", {}).get("items", {})
            if "propertyOrdering" in task_schema:
                task_schema["propertyOrdering"] = [key for key in task_schema["propertyOrdering"] if key in task_properties]
        if "intent" in properties and "geography" in properties:
            result["required"] = list(dict.fromkeys(result.get("required", []) + ["intent"]))
        if properties:
            order = list(properties)
            if "objective_mode" in properties:
                order = ["action", "answer_kind", "objective_mode", "answer_requirements"] + [key for key in order if key not in {"action", "answer_kind", "objective_mode", "answer_requirements"}]
            result["propertyOrdering"] = order
        return result

    return convert(schema)
