"""Resolve verified voters and households without rewriting their stored identifiers."""

import hashlib
import unicodedata
from contextlib import closing
from functools import lru_cache
from uuid import uuid4

from agents.contracts import SpecialistResult


def normalized_station(value):
    return " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())


@lru_cache(maxsize=64)
def _digest(filename, modified, size):
    digest = hashlib.sha256()
    with open(filename, "rb") as file:
        for chunk in iter(lambda: file.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def data_version(slug, data, filename="voters.db"):
    path = data.resolve_data_path(slug, filename)
    stat = path.stat()
    return _digest(str(path), stat.st_mtime_ns, stat.st_size)


def remember_voters(result, slug, data, context):
    """Mint references only for successful, uniquely reverified database records."""
    if result.agent != "voter" or result.status not in {"complete", "partial"}:
        return
    candidates = []
    for fact in result.facts:
        candidates.extend(fact.get("records", []))
        if "columns" in fact and "rows" in fact:
            candidates.extend(dict(zip(fact["columns"], row)) for row in fact["rows"])
    epics = list(dict.fromkeys(str(row["epic_id"]) for row in candidates if row.get("epic_id")))[:20]
    if not epics:
        return
    version = data_version(slug, data)
    if context.get("data_version") != version:
        context.clear()
        context.update({"data_version": version, "references": {}, "selected_reference": None})
    references = context.setdefault("references", {})
    with closing(data.open_sqlite(slug, "voters.db")) as conn:
        verified = []
        for epic in epics:
            rows = conn.execute("SELECT epic_id,name,house_no,polling_station FROM voters WHERE epic_id = ?", (epic,)).fetchmany(2)
            if len(rows) != 1:
                continue
            record = dict(rows[0])
            key = next((k for k, v in references.items() if v["epic_id"] == epic), None) or uuid4().hex
            references[key] = {**record, "constituency_id": slug, "data_version": version}
            verified.append({"reference": key, "name": record["name"], "polling_station": record["polling_station"]})
        if verified:
            # Expose selectable references, without selecting the first person in a list.
            if not any(fact.get("operation") == "household_lookup" for fact in result.facts):
                context["selected_reference"] = None
            result.facts.append({"voter_references": verified})
    # Keep a small bounded working set and retain an explicitly selected anchor.
    while len(references) > 40:
        key = next(k for k in references if k != context.get("selected_reference"))
        del references[key]


def household_lookup(task, slug, data, context):
    result = SpecialistResult(task_id=task.task_id, agent="voter", status="failed")
    query = task.household_query
    try:
        if task.constraints:
            raise ValueError("Put household filters in household_query; do not retain name or demographic filters")
        version = data_version(slug, data)
        epic, anchor = query.epic_id, None
        if query.reference:
            saved = context.get("references", {}).get(query.reference)
            if not saved or saved["constituency_id"] != slug:
                result.status = "needs_clarification"
                result.missing_inputs = ["Please identify the voter whose household you mean."]
                return result
            if saved["data_version"] != version:
                result.status = "needs_clarification"
                result.missing_inputs = ["The voter roll has changed. Please look up the voter again before using the previous household reference."]
                return result
            epic = saved["epic_id"]
        with closing(data.open_sqlite(slug, "voters.db")) as conn:
            if epic:
                rows = conn.execute("SELECT * FROM voters WHERE epic_id = ?", (epic,)).fetchmany(2)
                if len(rows) != 1:
                    result.status = "needs_clarification"
                    result.missing_inputs = ["The voter identifier does not resolve uniquely in this constituency. Please confirm the voter."]
                    return result
                anchor = dict(rows[0])
                house, station = anchor["house_no"], anchor["polling_station"]
            else:
                house = query.house_no.strip()
                stations = [row[0] for row in conn.execute("SELECT DISTINCT polling_station FROM voters")
                            if normalized_station(row[0]) == normalized_station(query.polling_station)]
                if len(stations) != 1:
                    result.status = "needs_clarification"
                    result.missing_inputs = ["Please confirm the exact polling station; the supplied station does not resolve uniquely."]
                    return result
                station = stations[0]
            if not str(house or "").strip() or not str(station or "").strip():
                raise ValueError("The voter does not have a usable house and polling station")
            # Preserve house punctuation. Never broaden to the entire constituency.
            where = "TRIM(CAST(house_no AS TEXT)) = ? AND polling_station = ?"
            params = [str(house).strip(), station]
            total = conn.execute(f"SELECT COUNT(*) FROM voters WHERE {where}", params).fetchone()[0]
            if anchor:
                included = conn.execute(f"SELECT COUNT(*) FROM voters WHERE {where} AND epic_id = ?", [*params, epic]).fetchone()[0]
                if included != 1:
                    raise ValueError("Household lookup contradicted its verified anchor; no zero-match answer is permitted")
            returned_total = total
            if query.exclude_anchor:
                where += " AND (epic_id IS NULL OR epic_id != ?)"
                params.append(epic)
                returned_total -= 1
            columns = set(data.list_columns(conn, "voters"))
            display = columns & {"name", "relative_name", "relation_type", "house_no", "age", "gender", "epic_id", "polling_station", "region"}
            select = ", ".join('"' + key + '"' for key in sorted(display))
            sql = f"SELECT {select} FROM voters WHERE {where} ORDER BY epic_id LIMIT 20"
            records = [dict(row) for row in conn.execute(sql, params).fetchall()]
            result.status = "complete"
            result.facts = [{"operation": "household_lookup", "house_no": house, "polling_station": station,
                             "total": returned_total, "household_total_including_anchor": total,
                             "excluded_anchor": query.exclude_anchor, "records": records,
                             "truncated": returned_total > 20}]
            result.sources = [{"database": "voters.db", "data_version": version, "sql": sql, "parameters": params,
                               "anchor_checked": bool(anchor)}]
            result.limitations = ["These are matching voter-roll records, not proof of current residence or family relationships."]
    except Exception as error:
        result.errors = [str(error)]
    return result


def name_predicate(sql):
    """Check SQLite name reads without mistaking SELECT/ORDER BY display columns for predicates."""
    # Tokenize strings, quoted identifiers, punctuation and SQL words; strings/comments are never identifiers.
    import re
    raw = re.findall(r"--[^\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[()]", sql)
    clause, stack = "select", []
    for token in raw:
        if token.startswith(("'", "--", "/*")):
            continue
        word = token.strip('"`[]').casefold()
        quoted = token.startswith(('"', '`', '['))
        if not quoted and word in {"where", "having", "on", "select", "order", "group", "limit", "union"}:
            clause = word
        elif word == "(":
            stack.append(clause)
        elif word == ")" and stack:
            clause = stack.pop()
        elif word in {"name", "relative_name"} and clause in {"where", "having", "on"}:
            return True
    return False
