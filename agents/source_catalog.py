"""Seat-specific schema/labels and reviewed equivalences, never inferred traits."""
import copy
import unicodedata
from collections import OrderedDict
from contextlib import closing
from threading import RLock

from agents.households import data_version

CATALOG_VERSION = "voter-catalog-v1"
CATEGORICAL = ("caste", "social_category", "religion", "gender", "relation_type")
DESCRIPTIONS = {
    "caste": "Specific stored caste classification; not interchangeable with social_category. Classification provenance may be unverified.",
    "social_category": "Broad stored category such as OBC, SC, ST or General; not a specific caste.",
    "religion": "Stored religion classification; do not infer from a person's name.",
    "gender": "Stored gender label.", "age": "Recorded age in years.",
    "name": "Voter name, not a demographic classification.",
    "relative_name": "Recorded relative's name, not a demographic classification.",
    "relation_type": "Recorded relationship type.", "polling_station": "Exact roll station label; resolve booth identifiers separately.",
    "region": "Stored locality label.", "house_no": "House identifier scoped to a station.",
    "epic_id": "Recorded voter identifier.", "serial_no": "Roll serial number.",
}
# Language equivalents only. No caste-to-category or surname-to-caste mappings.
# These apply only when a matching label actually exists in the selected source.
ALIASES = {
    "caste": [
        ("Yadav", "यादव"), ("Nishad", "निषाद"), ("Rajput", "राजपूत"),
        ("Brahmin", "Brahman", "ब्राह्मण"), ("Kurmi", "कुर्मी"),
        ("Pasi", "पासी"), ("Pal", "पाल"), ("Kushwaha", "कुशवाहा"),
    ],
    "social_category": [
        ("OBC", "Other Backward Class", "Other Backward Classes", "अन्य पिछड़ा वर्ग"),
        ("SC", "Scheduled Caste", "Scheduled Castes", "अनुसूचित जाति"),
        ("ST", "Scheduled Tribe", "Scheduled Tribes", "अनुसूचित जनजाति"),
        ("General", "सामान्य"),
    ],
    "religion": [("Muslim", "Muslims", "मुस्लिम", "मुसलमान"), ("Hindu", "Hindus", "हिन्दू", "हिंदू")],
    "gender": [("Female", "Women", "Woman", "महिला"), ("Male", "Men", "Man", "पुरुष")],
    "relation_type": [("Father", "पिता"), ("Husband", "पति"), ("Mother", "माता")],
}
_cache = OrderedDict()
_lock = RLock()


def normalized(value):
    return " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())


def equivalents(field, value):
    value = normalized(value)
    for group in ALIASES.get(field, []):
        values = {normalized(v) for v in group}
        if value in values:
            return values
    return {value}


def exact_labels(conn, field, value):
    """Resolve all source spellings of an exact/reviewed equivalent, even in large fields."""
    conn.create_function("category_normalized", 1, normalized, deterministic=True)
    values = sorted(equivalents(field, value))
    return [str(r[0]) for r in conn.execute(
        f'SELECT DISTINCT "{field}" FROM voters WHERE category_normalized("{field}") IN ({",".join("?" for _ in values)}) ORDER BY "{field}"', values)]


def catalog_from_connection(conn):
    columns = [{"name": r[1], "type": r[2], "meaning": DESCRIPTIONS.get(r[1], "Source column")}
               for r in conn.execute("PRAGMA table_info(voters)")]
    names = {c["name"] for c in columns}
    labels = {}
    for field in CATEGORICAL:
        if field not in names:
            continue
        rows = conn.execute(f'SELECT DISTINCT "{field}" FROM voters WHERE "{field}" IS NOT NULL ORDER BY "{field}" LIMIT 201').fetchall()
        labels[field] = {"values": [str(r[0]) for r in rows[:200]], "complete": len(rows) <= 200}
    aliases = {}
    for field, groups in ALIASES.items():
        if field in labels:
            aliases[field] = [{"aliases": list(group), "stored_values": [v for v in labels[field]["values"] if normalized(v) in equivalents(field, group[0])]}
                              for group in groups if any(normalized(v) in equivalents(field, group[0]) for v in labels[field]["values"])]
    return {"table": "voters", "columns": columns, "labels": labels, "aliases": aliases,
            "classification_provenance": "Stored classifications; individual identity/provenance is not independently verified.",
            "unknown_values": "NULL, empty and Undetermined are missing classifications, not matches for unknown user terms."}


def voter_catalog(slug, data):
    try:
        coverage = data.load_coverage(slug)
        if coverage.get("voter_available") is False:
            return {"constituency_id": slug, "available": False, "coverage": coverage}
        path = data.resolve_data_path(slug, "voters.db")
        version = data_version(slug, data)
        key = (slug, str(path.resolve()), version, CATALOG_VERSION)
        with _lock:
            cached = _cache.get(key)
            if cached is not None:
                _cache.move_to_end(key)
                return {**copy.deepcopy(cached), "coverage": coverage}
        with closing(data.open_sqlite(slug, "voters.db")) as conn:
            result = catalog_from_connection(conn)
        if not result["columns"]:
            return {"constituency_id": slug, "available": False, "reason": "voters table unavailable"}
        result.update(constituency_id=slug, available=True, data_version=version, catalog_version=CATALOG_VERSION)
        with _lock:
            _cache[key] = copy.deepcopy(result)
            while len(_cache) > 32:
                _cache.popitem(last=False)
        return {**result, "coverage": coverage}
    except (FileNotFoundError, OSError):
        return {"constituency_id": slug, "available": False, "reason": "voter source unavailable"}
