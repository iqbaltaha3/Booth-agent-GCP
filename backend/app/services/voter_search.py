"""Bounded, parameterized voter searches independent of the conversation agent."""

import json
import re
import time
import unicodedata
from collections import Counter
from contextlib import closing
from itertools import combinations

from agents.households import data_version, normalized_station
from agents.name_matching import name_matches, tokens, variants_for
from app.services import data
from app.voter_search_models import (
    SearchBooth, SearchCandidate, SearchCoverage, SearchOptions, SearchRequest,
    SearchResponse, VoterDetails,
)

CANDIDATE_LIMIT = 500
DETAIL_COLUMNS = tuple(VoterDetails.model_fields)
REQUIRED_COLUMNS = {"name", "relative_name", "house_no", "epic_id", "region", "polling_station"}


class SearchError(Exception):
    def __init__(self, code, status=422):
        self.code, self.status = code, status
        super().__init__(code)


def coverage_for(slug):
    value = data.load_coverage(slug)
    available = value.get("voter_available")
    return SearchCoverage(
        availability="unavailable" if available is False else "available" if available is True else "unknown",
        completeness="partial" if value.get("status") == "partial" else
        "declared" if value.get("status") == "complete_declared" else "unverified",
    )


def identifier(value):
    # House numbers remain text: preserve punctuation and leading zeros.
    return unicodedata.normalize("NFC", str("" if value is None else value)).strip().casefold()


def epic(value):
    return "".join(identifier(value).split()).upper()


def _check_schema(conn):
    if not REQUIRED_COLUMNS.issubset(data.list_columns(conn, "voters")):
        raise SearchError("schema_unavailable", 503)


def _booth_mapping(slug, conn):
    """Map only exact labels; a station shared by CSV parts is ambiguous."""
    pairs = [(str(r[0]), str(r[1] or "")) for r in conn.execute(
        "SELECT DISTINCT polling_station, region FROM voters WHERE polling_station IS NOT NULL"
    )]
    stations = {}
    for station, region in pairs:
        stations.setdefault(normalized_station(station), set()).add(station)
    regions = sorted({str(r[0]) for r in conn.execute(
        "SELECT DISTINCT region FROM voters WHERE region IS NOT NULL AND TRIM(region) != ''"
    )})
    try:
        rows = data.load_boothlist(slug)
    except FileNotFoundError:
        return regions, [], {}, False
    entries = []
    for row in rows:
        number = str(row.get("part_no") or row.get("part_number") or "").strip()
        name = str(row.get("pooling_station") or row.get("polling_station") or row.get("booth_name") or "").strip()
        digits = re.fullmatch(r"(?:part[ _-]*)?(\d+)", number, re.I)
        if not digits:
            continue  # No stable part identifier: never invent one from the label.
        part = f"Part_{int(digits[1])}"
        labels = {normalized_station(name), normalized_station(part), str(int(digits[1]))}
        matches = set().union(*(stations.get(label, set()) for label in labels if label))
        entries.append((part, name, str(row.get("region") or "").strip(), matches))
    references = Counter(s for _, _, _, matches in entries for s in matches)
    part_counts = Counter(part for part, _, _, _ in entries)
    booths, mapping = [], {}
    for part, name, csv_region, matches in entries:
        status = "unmapped" if not matches else "mapped"
        if len(matches) > 1 or any(references[s] > 1 for s in matches) or part_counts[part] > 1:
            status = "ambiguous"
        station = next(iter(matches)) if status == "mapped" else None
        # Region options use the voter roll, not unverified CSV geography.
        roll_regions = {r for s, r in pairs if s == station and r.strip()} if station else set()
        for region in sorted(roll_regions) or [csv_region]:
            booths.append(SearchBooth(booth_id=part, part_number=part, booth_name=name or part,
                                      region=region, mapping_status=status))
        if station:
            mapping[part] = station
    booths.sort(key=lambda b: (int(b.part_number.split("_")[1]), b.region))
    return regions, booths, mapping, True


def get_options(slug):
    coverage = coverage_for(slug)
    if coverage.availability == "unavailable":
        return SearchOptions(constituency_id=slug, coverage=coverage)
    with closing(data.open_sqlite(slug, "voters.db")) as conn:
        _check_schema(conn)
        regions, booths, _, available = _booth_mapping(slug, conn)
        return SearchOptions(constituency_id=slug, coverage=coverage, regions=regions, booths=booths,
                             booth_list_available=available, source_version=data_version(slug, data))


def _scope(request, conn, slug):
    if not request.region and not request.booth_id:
        return "1=1", [], {}
    regions, booths, mapping, _ = _booth_mapping(slug, conn)
    if request.region and request.region not in regions:
        raise SearchError("invalid_region")
    clauses, params = [], []
    if request.region:
        clauses.append('"region" = ?')
        params.append(request.region)
    if request.booth_id:
        choices = [b for b in booths if b.booth_id == request.booth_id]
        if not choices:
            raise SearchError("invalid_booth")
        if request.booth_id not in mapping:
            raise SearchError("booth_mapping_unavailable")
        if request.region and not any(b.region == request.region for b in choices):
            raise SearchError("booth_region_mismatch")
        clauses.append('"polling_station" = ?')
        params.append(mapping[request.booth_id])
    return " AND ".join(clauses) or "1=1", params, mapping


def _variants(request):
    result, status = {}, "not_needed" if request.spelling_assistance else "not_requested"
    if not request.spelling_assistance:
        return result, status
    from agents.model import chat
    # At most two provider calls, sharing a ten-second provider budget.
    end = time.monotonic() + 10
    for field in ("name", "relative_name"):
        value = getattr(request, field)
        if value and re.search(r"[A-Za-z]", value):
            try:
                def bounded_chat(messages, **kwargs):
                    return chat(messages, deadline=end, **kwargs)
                result[field] = variants_for(value, [], bounded_chat, json.loads)
                status = "used" if status != "unavailable" else status
            except Exception:
                # Optional provider assistance must never disable direct search.
                status = "unavailable"
    return result, status


def _field_status(field, stored, supplied, variants):
    if stored is None or not str(stored).strip():
        return "not_recorded"
    if field in {"name", "relative_name"}:
        if name_matches(stored, supplied, "full"):
            return "exact"
        if name_matches(stored, supplied, "word"):
            return "name_words"
        if any(name_matches(stored, v, "word") for v in variants.get(field, [])):
            return "spelling_variant"
        return "different"
    normalize = epic if field == "epic_id" else identifier
    return "exact" if normalize(stored) == normalize(supplied) else "different"


def search(request: SearchRequest, slug):
    coverage = coverage_for(slug)
    response = SearchResponse(constituency_id=slug, region=request.region, booth_id=request.booth_id,
                              coverage=coverage, outcome="unavailable")
    if coverage.availability == "unavailable":
        return response
    with closing(data.open_sqlite(slug, "voters.db")) as conn:
        _check_schema(conn)
        version = data_version(slug, data)
        response.source_version = version
        scope, scope_params, mapping = _scope(request, conn, slug)
        columns = set(data.list_columns(conn, "voters"))
        projection = ",".join(f'"{c}"' if c in columns else f'NULL AS "{c}"' for c in DETAIL_COLUMNS)
        select = f'SELECT rowid AS source_row, {projection} FROM voters WHERE ({scope})'
        conn.create_function("lookup_epic", 1, epic, deterministic=True)
        supplied = {f: getattr(request, f) for f in ("name", "relative_name", "house_no", "epic_id") if getattr(request, f)}
        if request.epic_id and not epic(request.epic_id):
            raise SearchError("invalid_epic")

        epic_rows = []
        if request.epic_id:
            epic_rows = conn.execute(select + ' AND lookup_epic(epic_id) = ? LIMIT ?',
                                     [*scope_params, epic(request.epic_id), CANDIDATE_LIMIT + 1]).fetchall()
        response.duplicate_epic = len(epic_rows) > 1
        variants = {}
        if epic_rows:
            rows = epic_rows
            response.spelling_status = "not_needed" if request.spelling_assistance else "not_requested"
        else:
            if any(f in {"name", "relative_name"} and not tokens(v) for f, v in supplied.items()):
                raise SearchError("invalid_name")
            variants, response.spelling_status = _variants(request)
            response.spelling_variants = variants
            fields = [f for f in supplied if f != "epic_id"]
            if request.search_mode == "name_only":
                fields = ["name"]
            elif request.search_mode == "relative_only":
                fields = ["relative_name"]
            minimum = 1 if len(fields) == 1 and request.booth_id and fields[0] != "house_no" else 2
            if len(fields) < minimum:
                rows = []
            else:
                clauses = []
                # UDF closures contain only this request's input, never cached records.
                for field in fields:
                    def matches(value, field=field):
                        return int(_field_status(field, value, supplied[field], variants)
                                   in {"exact", "name_words", "spelling_variant"})
                    conn.create_function(f"lookup_{field}", 1, matches, deterministic=True)
                    clauses.append(f'lookup_{field}("{field}") = 1')
                # Search all exact details first. Relax only when that set is empty.
                rows = conn.execute(select + " AND " + " AND ".join(clauses) + " LIMIT ?",
                                    [*scope_params, CANDIDATE_LIMIT + 1]).fetchall()
                if not rows and len(fields) > minimum:
                    subsets = ["(" + " AND ".join(group) + ")" for group in combinations(clauses, max(minimum, len(fields) - 1))]
                    rows = conn.execute(select + " AND (" + " OR ".join(subsets) + ") LIMIT ?",
                                        [*scope_params, CANDIDATE_LIMIT + 1]).fetchall()

        if request.booth_id and request.search_mode == "standard":
            response.suggested_modes = [mode for field, mode in (("name", "name_only"), ("relative_name", "relative_only"))
                                        if supplied.get(field)]
        if len(rows) > CANDIDATE_LIMIT:
            if data_version(slug, data) != version:
                raise SearchError("source_changed", 409)
            response.outcome, response.truncated = "refine_search", True
            return response

        if rows and not mapping:
            _, _, mapping, _ = _booth_mapping(slug, conn)
        ranked = []
        for row in rows:
            record = dict(row)
            statuses = {f: _field_status(f, record.get(f), value, variants) for f, value in supplied.items()}
            matched = sum(s in {"exact", "name_words", "spelling_variant"} for s in statuses.values())
            if epic_rows:
                tier = "epic_exact"
            elif request.search_mode != "standard" or len(supplied) == 1:
                tier = "single_name"
            elif matched < len(supplied):
                tier = "partial_details"
            else:
                tier = "all_details" if all(s == "exact" for s in statuses.values()) else "partial_name"
            voter = VoterDetails(**{c: str(record[c]) if record.get(c) is not None else None for c in DETAIL_COLUMNS})
            station = record.get("polling_station")
            booth = next((part for part, label in mapping.items() if label == station), None)
            candidate = SearchCandidate(record_id=f'{version[:16]}:{record["source_row"]}', voter=voter,
                                        booth_id=booth, match_tier=tier, field_matches=statuses,
                                        matched_fields=matched, supplied_fields=len(supplied))
            ranked.append(((-matched, -sum(s == "exact" for s in statuses.values()),
                            sum(s == "spelling_variant" for s in statuses.values()), record["source_row"]), candidate))
        ranked.sort(key=lambda item: item[0])
        response.candidates = [candidate for _, candidate in ranked[:request.page_size]]
        response.truncated = len(ranked) > request.page_size
        response.outcome = "matches" if ranked else "no_matches"
        # Records and source identifiers must describe the same source snapshot.
        if data_version(slug, data) != version:
            raise SearchError("source_changed", 409)
        if len(response.model_dump_json().encode()) > 128000:
            raise SearchError("response_too_large", 422)
        return response
