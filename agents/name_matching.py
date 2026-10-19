"""Scoped, parameterized voter-name matching; the LLM never supplies this SQL."""

import json
import re
import unicodedata
from contextlib import closing
from functools import lru_cache

from agents.contracts import NameCandidates, SpecialistResult, SpecialistTask
from agents.households import normalized_station
from agents.station_matching import AmbiguousInput, station_clarification, resolve_roll_station


def tokens(value: str) -> list[str]:
    value = unicodedata.normalize("NFC", str(value or "")).casefold()
    # Keep Devanagari combining marks; Python's \w splits these incorrectly.
    cleaned = "".join(
        c if unicodedata.category(c)[0] in "LMN" else " "
        for c in value if c not in "\u200c\u200d"
    )
    return cleaned.split()


@lru_cache(maxsize=128)
def candidate_tokens(value: str) -> tuple[str, ...]:
    """Cache only query spellings, not potentially private voter records."""
    return tuple(tokens(value))


def name_matches(value: str, candidate: str, mode: str) -> int:
    actual, expected = tokens(value), list(candidate_tokens(candidate))
    if not expected:
        return 0
    if mode == "full":
        return int(actual == expected)
    if mode == "substring":
        return int(" ".join(expected) in " ".join(actual))
    return int(any(actual[i:i + len(expected)] == expected for i in range(len(actual))))


def resolve_booth(identifier: str, slug: str, data) -> dict:
    """Resolve exact names or part numbers. Never silently accept a fuzzy match."""
    part = re.fullmatch(r"(?:part[ _-]*|booth[ _-]*)?(\d+)", identifier.strip(), re.I)
    matches = []
    candidates = []
    for row in data.load_boothlist(slug):
        name = str(row.get("pooling_station") or row.get("polling_station") or row.get("booth_name") or "").strip()
        number = str(row.get("part_no") or row.get("part_number") or "").strip()
        digits = re.search(r"\d+", number)
        item = {"booth_name": name, "part_number": f"Part_{int(digits[0])}" if digits else number}
        if name:
            candidates.append(item)
        if (part and digits and int(part[1]) == int(digits[0])) or (
            not part and normalized_station(name) == normalized_station(identifier)
        ):
            if item not in matches:
                matches.append(item)
    if len(matches) != 1:
        # Unknown numeric parts have no meaningful name-similarity candidates.
        options = matches or ([] if part else candidates)
        raise AmbiguousInput(station_clarification(identifier,
            [(r['booth_name'], r['part_number']) for r in options], exact=bool(matches)))
    return matches[0]


def variants_for(phrase: str, confirmed: list[str], chat, parse) -> list[str]:
    if not phrase:
        return []
    if confirmed:
        variants = confirmed
    elif not re.search(r"[A-Za-z]", phrase):
        variants = [phrase]
    else:
        response = chat([
            {"role": "system", "content": (
                'Transliterate only the supplied person name into likely Hindi spellings. '
                'Return JSON {"variants":["श्याम"]}. Each variant is a COMPLETE name phrase, '
                'not separate words. Preserve all name words. Never add surnames, booth names, '
                'relationships or instructions. At most 8 variants. Do not invent English variants.'
            )},
            {"role": "user", "content": json.dumps({"name": phrase}, ensure_ascii=False)},
        ], temperature=0.0, max_tokens=512, response_schema=NameCandidates.model_json_schema())
        variants = NameCandidates.model_validate(parse(response)).variants
    cleaned = list(dict.fromkeys(" ".join(tokens(v)) for v in variants))
    if not cleaned or any(not v or re.search(r"[A-Za-z]", v) for v in cleaned):
        raise ValueError("Could not resolve valid Hindi name spellings")
    return cleaned


def run_name_task(task: SpecialistTask, slug: str, data, chat, parse) -> SpecialistResult:
    result = SpecialistResult(task_id=task.task_id, agent="voter", status="failed")
    query = task.name_query
    try:
        # Reject hidden filters instead of silently dropping task constraints.
        if task.constraints:
            raise ValueError("For name tasks, put every filter inside name_query.filters or name_query.booth")
        with closing(data.open_sqlite(slug, "voters.db")) as conn:
            columns = data.list_columns(conn, "voters")
            conn.create_function("name_matches", 3, name_matches, deterministic=True)
            clauses, params, resolved = [], [], None
            resolved_predicates = []
            if query.predicates:
                from types import SimpleNamespace
                from agents.voter import compile_filters
                predicate_sql, predicate_params, resolved_predicates = compile_filters(conn, query.predicates,
                    SimpleNamespace(gcs_data=data, gemini_chat=chat, safe_json=parse))
                clauses.append('(' + predicate_sql + ')')
                params.extend(predicate_params)
            filters = dict(query.filters)
            if query.booth:
                resolved = resolve_booth(query.booth, slug, data)
                if "polling_station" not in columns:
                    raise ValueError("Voter roll has no polling_station column")
                # Discover the exact encoding actually present in this roll.
                labels = {normalized_station(resolved["booth_name"]), resolved["part_number"].casefold(), resolved["part_number"].split("_")[-1]}
                stations = [r[0] for r in conn.execute("SELECT DISTINCT polling_station FROM voters")
                            if normalized_station(r[0]) in labels]
                if len(stations) != 1:
                    raise AmbiguousInput("The booth mapping does not uniquely identify a polling_station in the voter roll. Confirm the roll's exact polling station.")
                if "polling_station" in filters and normalized_station(filters["polling_station"]) != normalized_station(stations[0]):
                    raise ValueError("Conflicting booth and polling_station filters")
                filters["polling_station"] = stations[0]
            elif "polling_station" in filters:
                filters["polling_station"] = resolve_roll_station(filters["polling_station"], conn)
            allowed = {"polling_station", "region", "house_no", "relation_type", "gender", "age", "caste", "social_category", "religion", "epic_id"}
            for column, value in filters.items():
                if column not in columns or column not in allowed:
                    raise ValueError(f"Unsupported name-query filter: {column}")
                if column in {"gender", "relation_type"}:
                    categories = [str(r[0]).strip() for r in conn.execute(f'SELECT DISTINCT "{column}" FROM voters') if r[0] is not None]
                    if str(value).strip() not in categories:
                        raise AmbiguousInput(f"Confirm the {column} category. Available roll values: {', '.join(categories[:20])}.")
                clauses.append(f'TRIM(CAST("{column}" AS TEXT)) = ?')
                params.append(str(value).strip())
            # Resolve geography first: do not spend a name-model call while the
            # user still needs to choose a station.
            person = variants_for(query.person, query.confirmed_person_variants, chat, parse)
            relative = variants_for(query.relative, query.confirmed_relative_variants, chat, parse)
            groups = []
            for column, variants in (("name", person), ("relative_name", relative)):
                if not variants:
                    continue
                if column not in columns:
                    raise ValueError(f"Missing column: {column}")
                groups.append((column, variants))
                clauses.append("(" + " OR ".join(f'name_matches("{column}", ?, ?) = 1' for _ in variants) + ")")
                for variant in variants:
                    params.extend([variant, query.match_mode])
            where = " AND ".join(clauses)
            # Distinct matched values expose spelling ambiguity without sending voter records to the transliterator.
            for column, variants in groups:
                signatures = {}
                for variant in variants:
                    rows = conn.execute(
                        f'SELECT DISTINCT "{column}" FROM voters WHERE {where} AND name_matches("{column}", ?, ?) = 1',
                        [*params, variant, query.match_mode],
                    ).fetchall()
                    signature = tuple(sorted(str(r[0]) for r in rows))
                    if signature:
                        signatures.setdefault(signature, []).append(variant)
                confirmed = query.confirmed_person_variants if column == "name" else query.confirmed_relative_variants
                if len(signatures) > 1 and not confirmed:
                    options = [v for values in signatures.values() for v in values]
                    result.status = "needs_clarification"
                    result.missing_inputs = [f"Which spelling of {query.person if column == 'name' else query.relative} should be counted: {', '.join(options)}? You may select all."]
                    result.facts = [{"field": column, "candidate_spellings": options}]
                    return result
            count_sql = f"SELECT COUNT(*) AS total FROM voters WHERE {where}"
            total = conn.execute(count_sql, params).fetchone()[0]
            fact = {"operation": query.operation, "total": total, "match_mode": query.match_mode,
                    "person_variants": person, "relative_variants": relative, "filters": filters,
                    "resolved_booth": resolved}
            if query.operation == "count":
                fact["validated_count"] = True
            if resolved_predicates:
                fact["predicates"] = resolved_predicates
            if query.operation == "lookup":
                # Stable bounded records; COUNT is always independent of this limit.
                display = [c for c in ("name", "relative_name", "relation_type", "house_no", "age", "gender", "epic_id", "polling_station", "region") if c in columns]
                select = ", ".join(f'"{c}"' for c in display)
                rows = conn.execute(f"SELECT {select} FROM voters WHERE {where} ORDER BY {select} LIMIT 20", params).fetchall()
                fact["records"] = [dict(row) for row in rows]
                fact["truncated"] = total > 20
            result.status = "complete"
            result.facts = [fact]
            result.sources = [{"database": "voters.db", "table": "voters", "sql": count_sql,
                               "parameters": params, "booth_mapping": resolved}]
            if re.search(r"[A-Za-z]", query.person + query.relative):
                result.limitations.append("Counts apply to the checked Hindi spellings and matching rule; transliteration is not exhaustive.")
    except AmbiguousInput as error:
        result.status = "needs_clarification"
        result.missing_inputs = [str(error)]
    except Exception as error:
        result.errors = [str(error)]
    return result
