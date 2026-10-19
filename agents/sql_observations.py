"""Small helpers for presenting executed SQLite observations to agents."""

from sqlglot import exp, parse_one


def literal_filters(conn, where, params):
    pieces = where.split('?')
    result = pieces[0]
    for value, piece in zip(params, pieces[1:]):
        result += conn.execute('SELECT quote(?)', (value,)).fetchone()[0] + piece
    return result


def count_observation(sql, observation):
    """Expose an executed scalar COUNT(*) AS total as observation metadata."""
    tree = parse_one(sql, read='sqlite')
    columns, rows = observation.get('columns', []), observation.get('rows', [])
    if not isinstance(tree, exp.Select) or tree.args.get('group') or tree.args.get('having'):
        return observation
    for projection in tree.expressions:
        if isinstance(projection, exp.Alias) and projection.alias.casefold() == 'total' and isinstance(projection.this, exp.Count) and isinstance(projection.this.this, exp.Star):
            if 'total' in columns and len(rows) == 1 and not observation.get('truncated'):
                value = rows[0][columns.index('total')]
                if isinstance(value, int) and value >= 0:
                    return {**observation, 'total': value}
    return observation
