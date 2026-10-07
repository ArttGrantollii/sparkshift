"""Translate a SQLGlot syntax tree into SparkShift's IR.

The translator decides what SparkShift can convert. It works from an
allowlist: it handles the parts of a query it understands and reports every
other part that is present as unsupported — including parts added by future
SQLGlot versions — so nothing is ever silently ignored. All unsupported parts
are collected before failing, so users see them at once.
"""

from sqlglot import exp

from sparkshift.diagnostics import Diagnostic
from sparkshift.errors import SQLParseError, UnsupportedSQLError
from sparkshift.ir import Relation, TableScan

_SELECT_PART_NAMES = {
    "with_": "WITH clause",
    "kind": "SELECT AS",
    "distinct": "DISTINCT",
    "joins": "JOIN",
    "laterals": "LATERAL",
    "pivots": "PIVOT",
    "where": "WHERE clause",
    "group": "GROUP BY clause",
    "having": "HAVING clause",
    "qualify": "QUALIFY clause",
    "windows": "WINDOW clause",
    "order": "ORDER BY clause",
    "limit": "LIMIT clause",
    "offset": "OFFSET clause",
}

_TABLE_PART_NAMES = {
    "alias": "table alias",
    "hints": "table hint",
    "sample": "TABLESAMPLE",
}

# Parts of a table reference that make up its name.
_TABLE_NAME_PARTS = frozenset({"this", "db", "catalog"})


def translate(tree: exp.Expression, dialect: str | None = None) -> Relation:
    """Translate one parsed statement into IR.

    ``dialect`` is the SQLGlot dialect the SQL was written in; it is used to
    render SQL fragments in diagnostics.

    Raises:
        SQLParseError: the statement is malformed in a way the parser accepted.
        UnsupportedSQLError: the statement uses unsupported constructs.
    """
    if not isinstance(tree, exp.Select):
        raise UnsupportedSQLError(
            [
                Diagnostic(
                    f"{tree.key.upper()} statement",
                    _fragment(tree, dialect),
                    hint="Only SELECT queries can be converted.",
                )
            ]
        )
    if not tree.expressions:
        # SQLGlot's grammar accepts "SELECT FROM t"; SQL does not.
        raise SQLParseError("SELECT has no columns.")

    issues: list[Diagnostic] = []
    source: Relation | None = None

    # Walk the parts in SQLGlot's declared order so diagnostics are deterministic.
    for part in exp.Select.arg_types:
        value = tree.args.get(part)
        if part == "expressions":
            _check_projection(value, dialect, issues)
        elif part == "from_":
            source = _translate_from(tree, dialect, issues)
        elif value:
            name = _SELECT_PART_NAMES.get(part, f"{part.strip('_').upper()} clause")
            for node in value if isinstance(value, list) else [value]:
                issues.append(Diagnostic(name, _fragment(node, dialect)))

    if issues:
        raise UnsupportedSQLError(issues)
    assert source is not None  # a FROM-less query always records an issue
    return source


def _check_projection(
    expressions: list[exp.Expression],
    dialect: str | None,
    issues: list[Diagnostic],
) -> None:
    if len(expressions) == 1 and isinstance(expressions[0], exp.Star):
        return
    columns = ", ".join(_fragment(expression, dialect) for expression in expressions)
    issues.append(Diagnostic("column list", columns))


def _translate_from(
    select: exp.Select,
    dialect: str | None,
    issues: list[Diagnostic],
) -> TableScan | None:
    from_ = select.args.get("from_")
    if from_ is None:
        issues.append(Diagnostic("SELECT without FROM", _fragment(select, dialect)))
        return None

    table = from_.this
    if not isinstance(table, exp.Table) or not isinstance(table.this, exp.Identifier):
        issues.append(
            Diagnostic("FROM source other than a table", _fragment(table, dialect))
        )
        return None

    supported = True
    for part in exp.Table.arg_types:
        if part not in _TABLE_NAME_PARTS and table.args.get(part):
            name = _TABLE_PART_NAMES.get(part, f"table {part}")
            issues.append(Diagnostic(name, _fragment(table, dialect)))
            supported = False
    if not supported:
        return None

    return TableScan(tuple(identifier.name for identifier in table.parts))


def _fragment(value: object, dialect: str | None) -> str:
    if isinstance(value, exp.Expression):
        return value.sql(dialect=dialect)
    return str(value)
