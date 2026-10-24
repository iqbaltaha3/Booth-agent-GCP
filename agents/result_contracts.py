"""Small result-level contracts for complete voter-roll lists and share filters.

These inspect executed observations. They do not approve or rewrite agent SQL.
"""

import re
from collections import defaultdict


_DIMENSIONS = {"region": ("region", "regions"), "polling_station": ("booth", "booths")}


def distinct_list_request(question):
    if not re.search(r"\b(?:list|show|find|give)\b.*\b(?:unique|distinct)\b", question, re.I):
        return None
    for field, words in _DIMENSIONS.items():
        if re.search(r"\b(?:" + "|".join(words) + r")\b", question, re.I):
            return field
    return None


def share_threshold_request(question):
    category = re.search(r"\b(muslim|hindu)\b", question, re.I)
    threshold = re.search(r"\b(?:more than|greater than|above|over|exceeding)\s+(\d+(?:\.\d+)?)\s*%|>\s*(\d+(?:\.\d+)?)\s*%", question, re.I)
    if not category or not threshold:
        return None
    for field, words in _DIMENSIONS.items():
        if re.search(r"\b(?:" + "|".join(words) + r")\b", question, re.I):
            return {"group_by": field, "category": category[1].title(),
                    "greater_than_percent": float(threshold[1] or threshold[2])}
    return None


def _observations(supported):
    for result in supported:
        sources = {source.get("source_id"): source for source in result.get("sources", [])}
        for fact in result.get("facts", []):
            yield fact, sources.get(fact.get("source_id"), {}).get("sql", "")


def _page_limit(sql):
    match = re.search(r"\bLIMIT\s+(-?\d+)", sql, re.I)
    return min(80, int(match[1])) if match and int(match[1]) > 0 else 80


def distinct_values(supported, field):
    """Return (values, complete) only for contiguous, ordered DISTINCT pages."""
    pages = defaultdict(list)
    for fact, sql in _observations(supported):
        distinct = re.search(r"^\s*SELECT\s+DISTINCT\s+[\"`]?" + field + r"[\"`]?\s+FROM\s+voters\b", sql, re.I)
        grouped = (re.search(r"^\s*SELECT\s+[\"`]?" + field + r"[\"`]?\s+FROM\s+voters\b", sql, re.I)
                   and re.search(r"\bGROUP\s+BY\s+[\"`]?" + field + r"[\"`]?(?:\s|$)", sql, re.I))
        if not (distinct or grouped):
            continue
        if not re.search(r"\bORDER\s+BY\s+[\"`]?" + field + r"[\"`]?(?:\s|$)", sql, re.I):
            continue
        if re.search(r"\bWHERE\b", sql, re.I):
            continue  # A filtered page cannot establish an all-values list.
        if [str(c).casefold() for c in fact.get("columns", [])] != [field]:
            continue
        offset = re.search(r"\bOFFSET\s+(\d+)", sql, re.I)
        pages[int(offset[1]) if offset else 0].append((fact, _page_limit(sql)))
    values = []
    offset = 0
    while offset in pages:
        fact, limit = pages[offset][0]
        rows = fact.get("rows", [])
        if any(len(row) != 1 for row in rows):
            break
        values.extend(str(row[0]) for row in rows if row[0] is not None and str(row[0]).strip())
        if not fact.get("truncated") and len(rows) < limit:
            return list(dict.fromkeys(values)), True
        if len(rows) != limit:
            break
        offset += len(rows)
    return list(dict.fromkeys(values)), False


def qualifying_shares(supported, contract):
    """Find a full grouped result with numerator and denominator in each row."""
    field = contract["group_by"]
    category = contract["category"].casefold()
    threshold = contract["greater_than_percent"]
    for fact, sql in reversed(list(_observations(supported))):
        columns = [str(name).casefold() for name in fact.get("columns", [])]
        if field not in columns or fact.get("truncated"):
            continue
        if not re.search(r"\bFROM\s+voters\b", sql, re.I) or not re.search(r"\bGROUP\s+BY\s+[\"`]?" + field + r"[\"`]?(?:\s|$)", sql, re.I):
            continue
        if not re.search(r"\breligion\b", sql, re.I) or category not in sql.casefold():
            continue
        if re.search(r"\bLIMIT\s+\d+", sql, re.I):
            continue  # An explicit limited result does not prove full coverage.
        numerator = next((i for i, name in enumerate(columns) if category in name and ("count" in name or "voter" in name or "population" in name)), None)
        denominator = next((i for i, name in enumerate(columns) if name in {"total", "total_count", "total_voters", "total_population", "region_total", "booth_total", "denominator"}), None)
        if numerator is None or denominator is None:
            continue
        accepted = []
        for row in fact.get("rows", []):
            try:
                total = int(row[denominator])
                matching = int(row[numerator])
            except (IndexError, TypeError, ValueError):
                return None
            if total <= 0 or matching < 0 or matching > total:
                return None
            name = row[columns.index(field)]
            if name is not None and str(name).strip() and matching * 100 > total * threshold:
                accepted.append((str(name), matching, total))
        return accepted
    return None


def shares_match_source(connection, contract, rows):
    """Check the final grouped values and coverage, without constraining SQL form."""
    field = contract["group_by"]  # Set only by the request recognizer.
    category = contract["category"]
    threshold = contract["greater_than_percent"]
    expected = {}
    sql = (f"SELECT {field}, COUNT(*) AS total_count, "
           "SUM(CASE WHEN religion = ? THEN 1 ELSE 0 END) AS matching_count "
           f"FROM voters GROUP BY {field}")
    for name, total, matching in connection.execute(sql, (category,)):
        if name is not None and str(name).strip() and matching * 100 > total * threshold:
            expected[str(name)] = (matching, total)
    actual = {name: (matching, total) for name, matching, total in rows}
    return len(actual) == len(rows) and actual == expected


def render_distinct(values, field, complete):
    label = "regions" if field == "region" else "polling stations"
    opening = f"Unique stored {label} ({len(values)}):" if complete else f"Partial list of unique stored {label} ({len(values)} so far):"
    return opening + "\n" + "\n".join(f"{index}. {value}" for index, value in enumerate(values, 1))


def render_shares(rows, contract):
    label = "Regions" if contract["group_by"] == "region" else "Polling stations"
    category = contract["category"]
    threshold = contract["greater_than_percent"]
    if not rows:
        return f"No {label.lower()} in the available voter roll have more than {threshold:g}% {category} voter records."
    lines = [f"{label} with more than {threshold:g}% {category} voter records:"]
    for index, (name, matching, total) in enumerate(sorted(rows), 1):
        lines.append(f"{index}. {name}: {matching / total * 100:.2f}% ({matching} of {total})")
    return "\n".join(lines)
