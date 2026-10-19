"""Structural checks for source access and supported analytical filter patterns.

This is deliberately not a proof of arbitrary SQL's natural-language meaning.
All answer queries are specialist-written, including counts and record lookups.
"""
import re
from decimal import Decimal, InvalidOperation
from sqlglot import parse_one, exp


def canonical(expression, integer_age=False):
    node = expression.copy()
    def clean(part):
        if isinstance(part, exp.Column):
            part.set('table', None)
        if isinstance(part, exp.Identifier):
            part.set('quoted', False)
        if isinstance(part, exp.Paren):
            return part.this
        if integer_age and isinstance(part, exp.Cast) and isinstance(part.this, exp.Column) and part.this.name.casefold() == 'age' and part.args.get('to').sql().upper() in {'FLOAT', 'DOUBLE', 'REAL'}:
            return part.this
        if isinstance(part, exp.In) and len(part.expressions) == 1:
            return exp.EQ(this=part.this, expression=part.expressions[0])
        if isinstance(part, exp.In) and len(part.expressions) > 1:
            part.set('expressions', sorted(part.expressions, key=lambda value: value.sql().casefold()))
        if isinstance(part, exp.Literal) and not part.is_string:
            try:
                return exp.Literal.number(format(Decimal(part.this).normalize(), 'f'))
            except InvalidOperation:
                pass
        return part
    for _ in range(3):
        node = node.transform(clean)
    return node.sql(dialect='sqlite', normalize=True)


def conjuncts(node, integer_age=False):
    if isinstance(node, exp.Paren):
        return conjuncts(node.this, integer_age)
    if isinstance(node, exp.And):
        return conjuncts(node.this, integer_age) + conjuncts(node.expression, integer_age)
    return [canonical(node, integer_age)]


def literal_filters(conn, where, params):
    pieces = where.split('?')
    result = pieces[0]
    for value, piece in zip(params, pieces[1:]):
        result += conn.execute('SELECT quote(?)', (value,)).fetchone()[0] + piece
    return result


def check_query(sql, task, conn, runner, compiled_filters=None):
    tree = parse_one(sql, read='sqlite')
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise ValueError('Only analytical SELECT queries are supported')
    allowed = {name.casefold() for name in runner.gcs_data.list_tables(conn)} | {'sqlite_master'}
    ctes = {cte.alias_or_name.casefold() for cte in tree.find_all(exp.CTE)}
    tables = list(tree.find_all(exp.Table))
    for table in tables:
        if table.catalog or table.db or table.name.casefold() not in allowed | ctes:
            raise ValueError('Query must use tables in the selected constituency source only')
    request = task.history_request
    if request and request.operation != 'coverage':
        if not any(re.fullmatch(r'form20_\d{4}_(lok|vidhan)', table.name, re.I) for table in tables):
            raise ValueError('Election-result evidence must read the requested Form-20 source, not return invented constants')
        for table in tables:
            match = re.fullmatch(r'form20_(\d{4})_(lok|vidhan)', table.name, re.I)
            if match and ((request.year and int(match[1]) != request.year) or
                          (request.election_type and match[2] != request.election_type)):
                raise ValueError('Query uses the wrong election year or type for history_request')
    request = task.voter_request
    if request and not any(t.name.casefold() == 'voters' for t in tables):
        raise ValueError('Voter evidence must read the voters source, not return invented constants')
    if request and request.group_by:
        grouped = {column.name.casefold() for group in tree.find_all(exp.Group) for column in group.find_all(exp.Column)}
        if not set(request.group_by) <= grouped:
            raise ValueError('SQL must preserve all requested grouping fields')
    if request and (compiled_filters is not None or request.filters or request.booth or request.any_of):
        from agents.voter import compile_filters
        if compiled_filters is None and request.booth:
            raise ValueError('Booth-scoped analysis requires the Voter-resolved station filter')
        where, params, _ = compiled_filters if compiled_filters is not None else compile_filters(conn, request.filters, runner)
        literal_where = literal_filters(conn, where, params)
        integer_age = any(row[1].casefold() == 'age' and 'INT' in row[2].upper() for row in conn.execute('PRAGMA table_info(voters)'))
        required = set(conjuncts(parse_one('SELECT * FROM voters WHERE ' + literal_where, read='sqlite').args['where'].this, integer_age))
        if required == {'1 = 1'}:
            required = set()
        for table in tables:
            if table.name.casefold() != 'voters':
                continue
            select = table.find_ancestor(exp.Select)
            actual = select.args.get('where') if select else None
            present = set(conjuncts(actual.this, integer_age)) if actual else set()
            if present == {'1 = 1'}:
                present = set()
            if required != present:
                raise ValueError('SQL must retain every resolved voter filter, without extra population restrictions, as mandatory conjunctions on each base voter scan. Use exact stored labels and a filtered base CTE; do not hide filters in OR branches.')
    return tree


def count_observation(sql, observation):
    """Tag a scalar source COUNT, never a model-supplied number or list length."""
    tree = parse_one(sql, read='sqlite')
    columns, rows = observation.get('columns', []), observation.get('rows', [])
    if not isinstance(tree, exp.Select) or tree.args.get('group') or tree.args.get('having'):
        return observation
    for projection in tree.expressions:
        if isinstance(projection, exp.Alias) and projection.alias.casefold() == 'total' and isinstance(projection.this, exp.Count) and isinstance(projection.this.this, exp.Star):
            if 'total' in columns and len(rows) == 1 and not observation.get('truncated'):
                value = rows[0][columns.index('total')]
                if isinstance(value, int) and value >= 0:
                    return {**observation, 'total': value, 'validated_count': True}
    return observation
