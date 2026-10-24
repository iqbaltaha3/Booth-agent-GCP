"""Name normalization, transliteration and unique booth resolution for Voter."""

import json
import re
import unicodedata
from functools import lru_cache

from agents.contracts import NameCandidates
from agents.households import normalized_station
from agents.station_matching import AmbiguousInput, station_clarification


def word_count(value: str | None) -> int:
    """Count whitespace-separated words; missing/blank roll values have none."""
    return len(str(value or "").split())


def tokens(value: str) -> list[str]:
    # Electoral rolls commonly vary only in whether Devanagari nukta is encoded
    # (फ़/फ, क़/क). Treat those forms alike while preserving word boundaries.
    value = unicodedata.normalize("NFD", str(value or "")).replace("\u093c", "")
    value = unicodedata.normalize("NFC", value).casefold()
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
                'Transliterate only the supplied person name into ALL likely Hindi spellings. '
                'Return JSON {"variants":["श्याम"]}. Each variant is a COMPLETE name phrase, '
                'not separate words. Preserve all name words. Never add surnames, booth names, '
                'relationships or instructions. Include common short/long vowel spelling variants. '
                'At most 8 variants. Do not invent English variants.'
            )},
            {"role": "user", "content": json.dumps({"name": phrase}, ensure_ascii=False)},
        ], temperature=0.0, max_tokens=512, response_schema=NameCandidates.model_json_schema())
        variants = NameCandidates.model_validate(parse(response)).variants
    cleaned = list(dict.fromkeys(" ".join(tokens(v)) for v in variants))
    if not cleaned or any(not v or re.search(r"[A-Za-z]", v) for v in cleaned):
        raise ValueError("Could not resolve valid Hindi name spellings")
    return cleaned
