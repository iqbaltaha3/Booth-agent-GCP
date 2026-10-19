"""Deterministic station suggestions; approximate matches never authorize a query."""

import unicodedata
from difflib import SequenceMatcher


class AmbiguousInput(ValueError):
    pass


def _words(value):
    value = unicodedata.normalize('NFC', str(value or '')).casefold()
    return ' '.join(''.join(c if unicodedata.category(c)[0] in 'LMN' else ' '
                            for c in value).split())


def station_clarification(identifier, candidates, *, exact=False):
    """Candidates are (name, part) pairs from the current authorized source only."""
    query = _words(identifier)
    ranked = []
    for name, part in set(candidates):
        label = _words(name)
        if not label:
            continue
        overlap = len(set(query.split()) & set(label.split())) / max(1, len(set(query.split())))
        score = max(SequenceMatcher(None, query, label).ratio(), overlap * .85)
        if query and (query in label or label in query):
            score = max(score, .9)
        if exact or (query and score >= .55):
            ranked.append((-score, str(name), str(part)))
    ranked.sort()
    if ranked:
        choices = [f'{index}. {name}' + (f' — {part}' if part else '')
                   for index, (_, name, part) in enumerate(ranked[:5], 1)]
        return ('Please choose the polling station by its exact name or booth/part number. '
                'These are possible matches, not a confirmed selection:\n' + '\n'.join(choices))
    return (f'No matching polling station was found for "{str(identifier)[:200]}". '
            'Please give a more complete station name or its booth/part number.')


def resolve_roll_station(identifier, conn):
    """Resolve a direct roll label, otherwise offer labels without executing a lookup."""
    def normalized(value):
        return ' '.join(unicodedata.normalize('NFC', str(value or '')).casefold().split())
    stations = [r[0] for r in conn.execute('SELECT DISTINCT polling_station FROM voters')
                if r[0] is not None and str(r[0]).strip()]
    matches = [s for s in stations if normalized(s) == normalized(identifier)]
    if len(matches) == 1:
        return matches[0]
    raise AmbiguousInput(station_clarification(identifier,
        [(str(s), '') for s in (matches or stations)], exact=bool(matches)))
