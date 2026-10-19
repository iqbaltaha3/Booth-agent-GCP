"""Resolve verified voters and households without rewriting their stored identifiers."""

import hashlib
import unicodedata
from contextlib import closing
from functools import lru_cache
from uuid import uuid4


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
        population = context.get("population")
        context.clear()
        context.update({"data_version": version, "references": {}, "selected_reference": None})
        if population and population.get("data_version") == version:
            context["population"] = population
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
