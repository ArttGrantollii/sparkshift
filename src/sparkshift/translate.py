"""Translate a SQLGlot syntax tree into SparkShift's IR.

The translator decides what SparkShift can convert. It works from an
allowlist: it handles the parts of a query it understands and reports every
other part that is present as unsupported — including parts added by future
SQLGlot versions — so nothing is ever silently ignored. All unsupported parts
are collected before failing, so users see them at once.
"""

from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import ClassVar

from sqlglot import exp

from sparkshift import ir
from sparkshift.diagnostics import Diagnostic
from sparkshift.errors import SQLParseError, UnsupportedSQLError

_SELECT_PART_NAMES = {
    "with_": "WITH clause",
    "kind": "SELECT AS",
    "distinct": "DISTINCT",
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
    "hints": "table hint",
    "sample": "TABLESAMPLE",
}

# Parts of a table reference SparkShift handles: its name and an alias.
_TABLE_SUPPORTED_PARTS = frozenset({"this", "db", "catalog", "alias"})

# Parts of a join SparkShift handles; anything else present is rejected.
_JOIN_SUPPORTED_PARTS = frozenset({"this", "on", "side", "kind", "using"})

_OUTER_JOIN_KINDS = {
    "LEFT": ir.JoinKind.LEFT,
    "RIGHT": ir.JoinKind.RIGHT,
    "FULL": ir.JoinKind.FULL,
}

_AGGREGATE_FUNCTIONS: dict[type[exp.Expression], ir.AggregateFunction] = {
    exp.Count: ir.AggregateFunction.COUNT,
    exp.Sum: ir.AggregateFunction.SUM,
    exp.Avg: ir.AggregateFunction.AVG,
    exp.Min: ir.AggregateFunction.MIN,
    exp.Max: ir.AggregateFunction.MAX,
}

_GROUP_PART_NAMES = {
    "grouping_sets": "GROUPING SETS",
    "cube": "CUBE",
    "rollup": "ROLLUP",
    "totals": "WITH TOTALS",
    "all": "GROUP BY ALL",
}

# Grouping constructs SQLGlot keeps inside the GROUP BY expression list.
_GROUPING_CONSTRUCTS = (exp.Rollup, exp.Cube, exp.GroupingSets)

# In these dialects a GROUP BY name always means an input column, never a
# SELECT alias.
_NO_GROUP_BY_ALIAS_DIALECTS = frozenset({"tsql", "oracle"})

# Dialects where GROUP BY 1 does not mean "the first SELECT item".
_GROUP_BY_POSITION_HINTS = {
    "oracle": "In Oracle, GROUP BY 1 groups by the constant 1, not by a column.",
    "tsql": "T-SQL does not support GROUP BY positions.",
}

# Readable names for parts of predicates and casts in diagnostics.
_PART_LABELS = {"query": "a subquery", "symmetric": "SYMMETRIC", "format": "FORMAT"}

_BACKSLASH_HINT = (
    "Whether backslash escapes LIKE wildcards differs between databases and Spark."
)
_TSQL_BRACKETS_HINT = (
    "T-SQL treats [ ] as a character class in LIKE patterns; Spark does not."
)
_TSQL_ISNULL_HINT = (
    "T-SQL's ISNULL returns the first argument's type, so ISNULL(int_col, 1.5) "
    "is 1, while COALESCE returns 1.5. Use COALESCE if that is intended."
)

# Cast targets whose meaning is the same in every supported dialect.
_SIMPLE_CAST_TYPES = {
    exp.DataType.Type.INT: "int",
    exp.DataType.Type.BIGINT: "bigint",
    exp.DataType.Type.SMALLINT: "smallint",
    exp.DataType.Type.TINYINT: "tinyint",
    exp.DataType.Type.DOUBLE: "double",
    exp.DataType.Type.DATE: "date",
    exp.DataType.Type.BOOLEAN: "boolean",
}
_STRING_CAST_TYPES = frozenset(
    {exp.DataType.Type.VARCHAR, exp.DataType.Type.NVARCHAR, exp.DataType.Type.TEXT}
)
_CAST_HINTS = {
    exp.DataType.Type.FLOAT: (
        "FLOAT and REAL have different sizes in different databases (T-SQL "
        "FLOAT is 8 bytes, Spark FLOAT is 4); cast to DOUBLE or DECIMAL(p, s)."
    ),
    exp.DataType.Type.VARCHAR: (
        "Length-limited strings truncate or pad differently across databases; "
        "cast to VARCHAR without a length."
    ),
    exp.DataType.Type.NVARCHAR: (
        "Length-limited strings truncate or pad differently across databases; "
        "cast to VARCHAR without a length."
    ),
    exp.DataType.Type.CHAR: (
        "CHAR(n) pads with spaces differently across databases; "
        "cast to VARCHAR without a length."
    ),
    exp.DataType.Type.ROWVERSION: (
        "In T-SQL, TIMESTAMP means ROWVERSION, an automatically generated "
        "binary row version, not a date and time."
    ),
    exp.DataType.Type.UTINYINT: (
        "This TINYINT is unsigned (0 to 255) and overflows differently from "
        "Spark's signed TINYINT; cast to SMALLINT."
    ),
}
# One-argument functions with the same meaning in every supported dialect,
# mapped to their pyspark.sql.functions names.
_SIMPLE_FUNCTIONS = {
    exp.Upper: "upper",
    exp.Lower: "lower",
    exp.Abs: "abs",
    exp.Ceil: "ceil",
    exp.Floor: "floor",
    exp.Sqrt: "sqrt",
    exp.Exp: "exp",
    exp.Ln: "ln",
    exp.Sign: "sign",
}
_TRIM_FUNCTIONS = {"BOTH": "trim", "LEADING": "ltrim", "TRAILING": "rtrim"}

# Dialects that round floating-point halves to even (ROUND(2.5::float) = 2)
# but exact numbers away from zero. Spark always rounds halves away from zero,
# and SparkShift cannot see whether a column is floating-point.
_HALF_EVEN_FLOAT_ROUND_DIALECTS = frozenset({"postgres", "mysql", "oracle"})

_ROUND_HINT = (
    "This dialect rounds floating-point halves to even but exact numbers away "
    "from zero; Spark always rounds away from zero, and SparkShift cannot see "
    "the column type."
)
_LOG_HINT = (
    "LOG(x) is the natural logarithm in some databases and base 10 in others; "
    "write LN(x) or LOG(base, x)."
)
_GREATEST_HINT = (
    "In this dialect it returns NULL when any argument is NULL; Spark ignores "
    "NULL arguments."
)
_BIGQUERY_TRIM_HINT = (
    "BigQuery's TRIM removes all Unicode whitespace, including tabs and "
    "newlines; Spark's removes only spaces."
)
_CONSTANT_HINT = (
    "Only constant values are supported, because negative and computed "
    "positions behave differently across databases."
)
_FUNCTION_ALIAS_HINT = (
    "Spark names this column after the exact function spelling (for example "
    "CEIL or CEILING), which SparkShift cannot reproduce; add an alias."
)

# Date parts with the same meaning in every supported dialect.
_DATE_PARTS = {
    "YEAR": "year",
    "QUARTER": "quarter",
    "MONTH": "month",
    "DAY": "dayofmonth",
    "HOUR": "hour",
    "MINUTE": "minute",
}
_DATE_PART_FUNCTIONS = {
    exp.Year: "year",
    exp.Quarter: "quarter",
    exp.Month: "month",
    exp.Day: "dayofmonth",
    exp.DayOfMonth: "dayofmonth",
    exp.Hour: "hour",
}
_WEEKDAY_HINT = (
    "Weekday and week numbers differ across databases (Sunday is 0 or 1; "
    "ISO or US weeks)."
)
_DATE_PART_HINTS = {
    "SECOND": "Some databases include fractions of a second and others do not.",
    "DOW": _WEEKDAY_HINT,
    "DAYOFWEEK": _WEEKDAY_HINT,
    "WEEKDAY": _WEEKDAY_HINT,
    "WEEK": _WEEKDAY_HINT,
    "ISOWEEK": _WEEKDAY_HINT,
}
_DAY_UNITS = frozenset({"DAY", "DAYS"})
_KNOWN_DATE_UNITS = _DAY_UNITS | {
    "YEAR",
    "QUARTER",
    "MONTH",
    "WEEK",
    "HOUR",
    "MINUTE",
    "SECOND",
    "MILLISECOND",
    "MICROSECOND",
    "NANOSECOND",
}
_DATEDIFF_FORM_HINT = (
    "In this dialect DATEDIFF takes two arguments, DATEDIFF(end, start), and "
    "counts days."
)
_INTERVAL_UNITS = {
    "DAY": "days",
    "DAYS": "days",
    "WEEK": "weeks",
    "WEEKS": "weeks",
    "MONTH": "months",
    "MONTHS": "months",
    "YEAR": "years",
    "YEARS": "years",
}
_TRUNC_UNITS = frozenset({"YEAR", "QUARTER", "MONTH"})
_NO_DATEDIFF_DIALECTS = frozenset({"postgres", "oracle"})
_NO_DATEDIFF_HINT = "This dialect has no DATEDIFF function."
_DATEDIFF_UNIT_HINT = (
    "Only day differences are supported: databases count month and year "
    "boundaries differently."
)
_SYSDATE_HINT = (
    "Oracle's SYSDATE has no fractional seconds and uses the database server's "
    "time zone; use CURRENT_TIMESTAMP."
)
_ADD_MONTHS_HINT = (
    "ADD_MONTHS keeps the last day of the month in Oracle and Snowflake "
    "(Feb 29 + 1 month = Mar 31) but not in Spark; use + INTERVAL '1' MONTH."
)
_DATE_TRUNC_HINT = (
    "Truncation returns the input's type in this dialect (or does not exist), "
    "and SparkShift cannot see column types. PostgreSQL's DATE_TRUNC and "
    "BigQuery's DATE_TRUNC are supported."
)
_TSQL_IMPRECISE_DATETIMES = frozenset(
    {exp.DataType.Type.DATETIME, exp.DataType.Type.SMALLDATETIME}
)
_TSQL_DATETIME_HINT = (
    "T-SQL DATETIME rounds to 1/300 of a second and SMALLDATETIME to the "
    "minute; Spark timestamps keep microseconds. Use DATETIME2."
)

_TSQL_VARCHAR_HINT = (
    "In T-SQL, CAST to VARCHAR without a length means VARCHAR(30) and "
    "truncates longer values; Spark strings are unbounded."
)
_DECIMAL_DEFAULT_HINT = (
    "DECIMAL without a precision has a different default in each database; "
    "write DECIMAL(p, s)."
)

_ORACLE_OUTER_JOIN_HINT = (
    "Rewrite the (+) marker as an explicit LEFT or RIGHT JOIN ... ON; "
    "ignoring it would turn the outer join into an inner join."
)

_BINARY_OPERATORS: dict[type[exp.Expression], ir.BinaryOperator] = {
    exp.Add: ir.BinaryOperator.ADD,
    exp.Sub: ir.BinaryOperator.SUBTRACT,
    exp.Mul: ir.BinaryOperator.MULTIPLY,
    exp.Div: ir.BinaryOperator.DIVIDE,
    exp.Mod: ir.BinaryOperator.MODULO,
    exp.EQ: ir.BinaryOperator.EQUAL,
    exp.NEQ: ir.BinaryOperator.NOT_EQUAL,
    exp.LT: ir.BinaryOperator.LESS,
    exp.LTE: ir.BinaryOperator.LESS_EQUAL,
    exp.GT: ir.BinaryOperator.GREATER,
    exp.GTE: ir.BinaryOperator.GREATER_EQUAL,
    exp.And: ir.BinaryOperator.AND,
    exp.Or: ir.BinaryOperator.OR,
}

_INTEGER_DIVISION_HINT = (
    "Dividing two integers discards the remainder in this dialect but not in "
    "Spark, and SparkShift cannot see column types."
)
_SAFE_DIVISION_HINT = (
    "Division by zero returns NULL in this dialect but raises an error in Spark."
)
_TSQL_PLUS_HINT = (
    "T-SQL uses + for both addition and string concatenation, and SparkShift "
    "cannot see column types."
)


def translate(tree: exp.Expression, dialect: str | None = None) -> ir.Relation:
    """Translate one parsed statement into IR.

    ``dialect`` is the SQLGlot dialect the SQL was written in. It affects how
    some operators are interpreted and how SQL fragments are shown in
    diagnostics.

    Raises:
        SQLParseError: the statement is malformed in a way the parser accepted.
        UnsupportedSQLError: the statement uses unsupported constructs.
    """
    return _Translator(dialect).statement(tree)


class _Translator:
    def __init__(self, dialect: str | None) -> None:
        self.dialect = dialect
        self.issues: list[Diagnostic] = []
        # Where aggregate functions are currently not allowed, for diagnostics
        # (for example "WHERE"); None while translating where they are allowed.
        self.no_aggregates_in: str | None = None

    def statement(self, tree: exp.Expression) -> ir.Relation:
        if not isinstance(tree, exp.Select):
            self.unsupported(
                f"{tree.key.upper()} statement",
                tree,
                hint="Only SELECT queries can be converted.",
            )
            raise UnsupportedSQLError(self.issues)
        if not tree.expressions:
            # SQLGlot's grammar accepts "SELECT FROM t"; SQL does not.
            raise SQLParseError("SELECT has no columns.")

        relation = self.select(tree)
        if self.issues:
            raise UnsupportedSQLError(self.issues)
        assert relation is not None  # every failure path records an issue
        return relation

    # --- Queries ---------------------------------------------------------

    def select(self, select: exp.Select) -> ir.Relation | None:
        source: ir.Relation | None = None
        items: tuple[ir.Expression, ...] | None = None
        condition: ir.Expression | None = None
        group: exp.Group | None = None
        having: exp.Having | None = None
        distinct = False
        limit: int | None = None
        failed = False
        aggregating = _is_aggregate_query(select)

        # Walk the parts in SQLGlot's declared order so diagnostics are
        # deterministic.
        for part in exp.Select.arg_types:
            value = select.args.get(part)
            if part == "expressions":
                self.no_aggregates_in = None if aggregating else "SELECT"
                items = self.projection(value)
                self.no_aggregates_in = None
            elif part == "from_":
                source = self.from_(select)
            elif not value:
                continue
            elif part == "joins":
                source = self.joins(source, value)
                failed |= source is None
            elif part == "where":
                condition = self.where(value, select)
                failed |= condition is None
            elif part == "group":
                group = value
            elif part == "having":
                having = value
            elif part == "distinct":
                distinct = self.distinct(value)
                failed |= not distinct
            elif part == "limit":
                limit = self.limit(value)
                failed |= limit is None
            else:
                name = _SELECT_PART_NAMES.get(part, f"{part.strip('_').upper()} clause")
                for node in value if isinstance(value, list) else [value]:
                    self.unsupported(name, node)

        if source is None or items is None or failed:
            return None

        # Build the plan in SQL's logical evaluation order, not the order the
        # clauses are written in: FROM (and joins), WHERE, GROUP BY, HAVING,
        # SELECT, DISTINCT, LIMIT.
        relation: ir.Relation | None = source
        if condition is not None:
            relation = ir.Filter(source, condition)
        if aggregating:
            relation = self.aggregation(select, relation, items, group, having)
        elif items != (ir.Star(),):
            relation = ir.Project(relation, items)
        if relation is None:
            return None
        if distinct:
            relation = ir.Distinct(relation)
        if limit is not None:
            relation = ir.Limit(relation, limit)
        return relation

    def where(self, where: exp.Where, select: exp.Select) -> ir.Expression | None:
        issues_before = len(self.issues)
        if self.dialect == "snowflake":
            self.check_alias_references(where, select)
        condition = self.expression_without_aggregates(where.this, "WHERE")
        return None if len(self.issues) > issues_before else condition

    def check_alias_references(self, where: exp.Where, select: exp.Select) -> None:
        """Reject WHERE references to SELECT-list aliases.

        Snowflake lets WHERE refer to an alias defined in the SELECT list; Spark
        does not. Without the table schema, SparkShift cannot tell whether a
        name means the alias or a real column, so it does not guess.
        """
        aliases = {
            node.alias.lower()
            for node in select.expressions
            if isinstance(node, exp.Alias)
        }
        for column in where.find_all(exp.Column):
            if not column.table and column.name.lower() in aliases:
                self.unsupported(
                    "WHERE reference to a SELECT alias",
                    column,
                    hint="Repeat the aliased expression in the WHERE clause.",
                )

    def distinct(self, node: exp.Distinct) -> bool:
        if node.args.get("on"):
            self.unsupported("DISTINCT ON", node)
            return False
        return True

    def limit(self, node: exp.Expression) -> int | None:
        """Translate LIMIT n, T-SQL's TOP n, or FETCH FIRST n ROWS ONLY."""
        if isinstance(node, exp.Fetch):
            count, allowed = (
                node.args.get("count"),
                {"direction", "count", "limit_options"},
            )
        else:
            count, allowed = (
                node.args.get("expression"),
                {"expression", "limit_options"},
            )

        supported = True
        options = node.args.get("limit_options")
        if options is not None and options.args.get("percent"):
            self.unsupported("row limit in PERCENT", node)
            supported = False
        if options is not None and options.args.get("with_ties"):
            self.unsupported("row limit WITH TIES", node)
            supported = False
        for part, value in node.args.items():
            if value and part not in allowed:
                self.unsupported(f"row limit {part.upper()}", node)
                supported = False

        is_count = (
            isinstance(count, exp.Literal)
            and not count.is_string
            and count.this.isdigit()
        )
        if not is_count:
            self.unsupported(
                "row limit that is not a non-negative integer",
                node,
                hint="Use a constant number of rows.",
            )
            return None
        return int(count.this) if supported else None

    def from_(self, select: exp.Select) -> ir.Relation | None:
        from_ = select.args.get("from_")
        if from_ is None:
            self.unsupported("SELECT without FROM", select)
            return None
        # A joined table always gets a name, so qualified columns resolve.
        has_joins = bool(select.args.get("joins"))
        return self.table(from_.this, "FROM", always_alias=has_joins)

    def table(
        self, node: exp.Expression, clause: str, *, always_alias: bool
    ) -> ir.Relation | None:
        """Translate a table reference, with its alias if it has one."""
        if not isinstance(node, exp.Table) or not isinstance(node.this, exp.Identifier):
            self.unsupported(f"{clause} source other than a table", node)
            return None

        supported = True
        for part in exp.Table.arg_types:
            if part not in _TABLE_SUPPORTED_PARTS and node.args.get(part):
                self.unsupported(_TABLE_PART_NAMES.get(part, f"table {part}"), node)
                supported = False
        alias = node.args.get("alias")
        if alias is not None and alias.args.get("columns"):
            self.unsupported("table alias with column names", node)
            supported = False
        if not supported:
            return None

        scan = ir.TableScan(tuple(identifier.name for identifier in node.parts))
        if node.alias:
            return ir.RelationAlias(scan, node.alias)
        if always_alias:
            return ir.RelationAlias(scan, node.name)
        return scan

    def joins(
        self, left: ir.Relation | None, joins: list[exp.Join]
    ) -> ir.Relation | None:
        # SQL joins associate left to right: a JOIN b JOIN c is (a JOIN b) JOIN c.
        for join in joins:
            left = self.join(left, join)
        return left

    def join(self, left: ir.Relation | None, node: exp.Join) -> ir.Relation | None:
        issues_before = len(self.issues)
        right = self.table(node.this, "JOIN", always_alias=True)
        kind = self.join_kind(node)

        on = node.args.get("on")
        condition = (
            self.expression_without_aggregates(on, "a JOIN condition")
            if on is not None
            else None
        )
        using = tuple(identifier.name for identifier in node.args.get("using") or [])

        if left is None or right is None or kind is None:
            return None
        if len(self.issues) > issues_before:
            return None
        return ir.Join(left, right, kind, condition, using)

    def join_kind(self, node: exp.Join) -> ir.JoinKind | None:
        """Map SQLGlot's side and kind of a join to a JoinKind, or report why
        it is unsupported."""
        for part, value in node.args.items():
            if value and part not in _JOIN_SUPPORTED_PARTS:
                name = f"{node.method} JOIN" if part == "method" else f"JOIN {part}"
                self.unsupported(name, node)
                return None

        side, kind = node.side.upper(), node.kind.upper()
        # ON and USING together is a parse error, so at most one is present.
        has_condition = bool(node.args.get("on") or node.args.get("using"))
        if kind in ("SEMI", "ANTI"):
            self.unsupported(f"{side} {kind} JOIN".strip(), node)
            return None
        if kind == "CROSS":
            if has_condition:
                self.unsupported("CROSS JOIN with a condition", node)
                return None
            return ir.JoinKind.CROSS
        if side in _OUTER_JOIN_KINDS and kind in ("", "OUTER"):
            if not has_condition:
                self.unsupported(f"{side} JOIN without ON or USING", node)
                return None
            return _OUTER_JOIN_KINDS[side]
        if not side and kind in ("", "INNER"):
            if has_condition:
                return ir.JoinKind.INNER
            if not kind:
                # "FROM a, b" — and MySQL's "a JOIN b" without ON, which SQLGlot
                # represents identically — is a cross join.
                return ir.JoinKind.CROSS
            self.unsupported(
                "INNER JOIN without ON or USING",
                node,
                hint="Write CROSS JOIN for a Cartesian product.",
            )
            return None
        self.unsupported(f"{side} {kind} JOIN".strip(), node)
        return None

    def projection(
        self, expressions: list[exp.Expression]
    ) -> tuple[ir.Expression, ...] | None:
        items = [self.projection_item(expression) for expression in expressions]
        if any(item is None for item in items):
            return None
        return tuple(item for item in items if item is not None)

    def projection_item(self, node: exp.Expression) -> ir.Expression | None:
        if isinstance(node, exp.Star):
            return ir.Star()
        if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
            # "c.*": every column of one input table.
            qualifier = tuple(
                node.args[part].name
                for part in ("catalog", "db", "table")
                if node.args.get(part)
            )
            return ir.Star(qualifier)
        if isinstance(node, exp.Alias):
            expression = self.expression(node.this)
            return None if expression is None else ir.Alias(expression, node.alias)

        expression = self.expression(node)
        if (
            isinstance(expression, ir.UnaryOp)
            and expression.op is ir.UnaryOperator.NEGATE
        ):
            # Spark SQL names this column "(- x)" but PySpark names it
            # "negative(x)"; an explicit alias makes the output name defined.
            self.unsupported(
                "negation without an alias",
                node,
                hint="Add an alias, for example: -amount AS negative_amount.",
            )
            return None
        if expression is not None and _calls_function(expression):
            self.unsupported(
                "function result without an alias", node, _FUNCTION_ALIAS_HINT
            )
            return None
        unnamed = _differently_named(node)
        if expression is not None and unnamed is not None:
            # Spark SQL and PySpark generate different names for this column.
            self.unsupported(
                f"{unnamed} without an alias",
                node,
                hint="Add an alias, for example: ... AS flag.",
            )
            return None
        return expression

    # --- Aggregation -----------------------------------------------------

    def aggregation(
        self,
        select: exp.Select,
        source: ir.Relation,
        items: tuple[ir.Expression, ...],
        group: exp.Group | None,
        having: exp.Having | None,
    ) -> ir.Relation | None:
        """Translate GROUP BY, aggregates, and HAVING.

        PySpark's ``groupBy(...).agg(...)`` outputs the key columns first and
        then the aggregates. SQL allows any order and may leave keys out, so a
        final projection restores the SELECT list when the two shapes differ.
        """
        keys = self.group_keys(group, select)
        if keys is None:
            return None

        key_names: dict[ir.Column, str] = {}
        aggregates: list[ir.Expression] = []
        aggregate_nodes: list[exp.Expression] = []
        # Where each SELECT item comes from: ("key", n) or ("aggregate", n).
        layout: list[tuple[str, int]] = []
        failed = False
        for item, node in zip(items, select.expressions, strict=True):
            if isinstance(item, ir.Star):
                self.unsupported("SELECT * with aggregation", node)
                failed = True
                continue
            plain = _plain_column(item)
            key = _matching_key(plain[0], keys) if plain else None
            if plain and key is not None and key not in key_names:
                key_names[key] = plain[1]
                layout.append(("key", keys.index(key)))
                continue
            failed |= not self.check_grouped(item, keys)
            layout.append(("aggregate", len(aggregates)))
            aggregates.append(item)
            aggregate_nodes.append(node)

        def output_name(key: ir.Column) -> str:
            return key_names.get(key, key.name_parts[-1])

        helpers: list[ir.Expression] = []
        condition = None
        if having is not None:
            condition = self.having(
                having, select, keys, output_name, aggregates, helpers
            )
            failed |= condition is None
        if failed:
            return None

        grouped_keys = tuple(
            key
            if output_name(key) == key.name_parts[-1]
            else ir.Alias(key, output_name(key))
            for key in keys
        )
        relation: ir.Relation = ir.Aggregate(
            source, grouped_keys, (*aggregates, *helpers)
        )
        if condition is not None:
            relation = ir.Filter(relation, condition)

        natural = [("key", n) for n in range(len(keys))]
        natural += [("aggregate", n) for n in range(len(aggregates))]
        if layout == natural and not helpers:
            return relation

        columns: list[ir.Expression] = []
        for kind, index in layout:
            if kind == "key":
                columns.append(ir.Column((output_name(keys[index]),)))
            elif isinstance(aggregates[index], ir.Alias):
                columns.append(ir.Column((aggregates[index].name,)))  # type: ignore[union-attr]
            else:
                self.unsupported(
                    "aggregate without an alias in a reordered SELECT list",
                    aggregate_nodes[index],
                    hint="Add an alias, for example: COUNT(*) AS orders.",
                )
                failed = True
        return None if failed else ir.Project(relation, tuple(columns))

    def group_keys(
        self, group: exp.Group | None, select: exp.Select
    ) -> list[ir.Column] | None:
        if group is None:
            return []
        failed = False
        for part, value in group.args.items():
            if value and part != "expressions":
                self.unsupported(_GROUP_PART_NAMES.get(part, f"GROUP BY {part}"), group)
                failed = True

        keys: list[ir.Column] = []
        for node in group.expressions:
            key = self.group_key(node, select)
            if key is None:
                failed = True
            elif key not in keys:
                keys.append(key)
        return None if failed else keys

    def group_key(self, node: exp.Expression, select: exp.Select) -> ir.Column | None:
        """Translate one GROUP BY entry: a column, a position, or an alias."""
        if isinstance(node, exp.Literal) and not node.is_string and node.this.isdigit():
            target = self.group_position(node, select)
            if target is None:
                return None
            node = target

        if isinstance(node, exp.Column) and not isinstance(node.this, exp.Star):
            if self.dialect not in _NO_GROUP_BY_ALIAS_DIALECTS and not node.table:
                node = self.group_alias(node, select)
            return self.column(node) if node is not None else None

        name = node.key.upper() if isinstance(node, _GROUPING_CONSTRUCTS) else None
        self.unsupported(
            name or "GROUP BY expression",
            node,
            hint=None if name else "Only columns can be grouped by so far.",
        )
        return None

    def group_position(
        self, node: exp.Literal, select: exp.Select
    ) -> exp.Column | None:
        """Resolve GROUP BY 1 to the first SELECT item, which must be a column."""
        hint = _GROUP_BY_POSITION_HINTS.get(self.dialect or "")
        if hint is not None:
            self.unsupported("GROUP BY position", node, hint=hint)
            return None
        position = int(node.this)
        if not 1 <= position <= len(select.expressions):
            self.unsupported("GROUP BY position out of range", node)
            return None
        target = select.expressions[position - 1]
        if isinstance(target, exp.Alias):
            target = target.this
        if not isinstance(target, exp.Column) or isinstance(target.this, exp.Star):
            self.unsupported(
                "GROUP BY position of an expression",
                node,
                hint="Only columns can be grouped by so far.",
            )
            return None
        return target

    def group_alias(self, node: exp.Column, select: exp.Select) -> exp.Column | None:
        """Resolve a GROUP BY name that matches a SELECT alias.

        Databases resolve such a name to a same-named input column if one
        exists, and to the alias otherwise. Without the schema, SparkShift
        cannot tell which applies, unless the alias names a column of the same
        name, where both readings agree.
        """
        for item in select.expressions:
            name = node.name.lower()
            if not isinstance(item, exp.Alias) or item.alias.lower() != name:
                continue
            target = item.this
            if isinstance(target, exp.Column) and target.name.lower() == name:
                return target
            self.unsupported(
                "GROUP BY name that is also a SELECT alias",
                node,
                hint="Group by the expression itself, or rename the alias.",
            )
            return None
        return node

    def check_grouped(self, item: ir.Expression, keys: list[ir.Column]) -> bool:
        """Report columns used outside an aggregate that are not GROUP BY keys."""
        ok = True
        for column in _free_columns(item):
            if _matching_key(column, keys) is None:
                self.issues.append(
                    Diagnostic(
                        "column that is neither grouped nor aggregated",
                        ".".join(column.name_parts),
                        "Add it to GROUP BY or wrap it in an aggregate such as MAX.",
                    )
                )
                ok = False
        return ok

    def having(
        self,
        having: exp.Having,
        select: exp.Select,
        keys: list[ir.Column],
        output_name: Callable[[ir.Column], str],
        aggregates: list[ir.Expression],
        helpers: list[ir.Expression],
    ) -> ir.Expression | None:
        """Translate HAVING into a filter on the aggregated result.

        Aggregates already in the SELECT list are referred to by their alias;
        others are computed as helper columns, which a final projection drops.
        """
        issues_before = len(self.issues)
        condition = self.expression(having.this)
        if condition is None or len(self.issues) > issues_before:
            return None
        aliases = {
            node.alias.lower()
            for node in select.expressions
            if isinstance(node, exp.Alias)
        }

        def replace(expression: ir.Expression) -> ir.Expression | None:
            if isinstance(expression, ir.AggregateCall):
                for aggregate in aggregates:
                    if (
                        isinstance(aggregate, ir.Alias)
                        and aggregate.expression == expression
                    ):
                        return ir.Column((aggregate.name,))
                for helper in helpers:
                    assert isinstance(helper, ir.Alias)
                    if helper.expression == expression:
                        return ir.Column((helper.name,))
                helpers.append(ir.Alias(expression, f"_having_{len(helpers) + 1}"))
                return ir.Column((helpers[-1].name,))  # type: ignore[union-attr]
            if isinstance(expression, ir.Column):
                key = _matching_key(expression, keys)
                if key is not None:
                    return ir.Column((output_name(key),))
                name = ".".join(expression.name_parts)
                if name.lower() in aliases:
                    message = "HAVING reference to a SELECT alias"
                    hint = "Repeat the aggregate expression in HAVING."
                else:
                    message = "column that is neither grouped nor aggregated"
                    hint = "Add it to GROUP BY or wrap it in an aggregate such as MAX."
                self.issues.append(Diagnostic(message, name, hint))
                return expression
            return None

        rewritten = _rewrite(condition, replace)
        return None if len(self.issues) > issues_before else rewritten

    def aggregate_call(
        self, node: exp.AggFunc, function: ir.AggregateFunction
    ) -> ir.AggregateCall | None:
        name = function.name
        if self.no_aggregates_in is not None:
            context = self.no_aggregates_in
            hint = "Filter on aggregates with HAVING." if context == "WHERE" else None
            self.unsupported(f"aggregate function {name} in {context}", node, hint)
            return None
        for part, value in node.args.items():
            if value and part not in ("this", "big_int"):
                self.unsupported(f"{name} with {part.upper()}", node)
                return None

        argument = node.this
        distinct = isinstance(argument, exp.Distinct)
        if distinct:
            if function not in (ir.AggregateFunction.COUNT, ir.AggregateFunction.SUM):
                self.unsupported(f"{name}(DISTINCT ...)", node)
                return None
            operands = list(argument.expressions)
        elif isinstance(argument, exp.Star) and function is ir.AggregateFunction.COUNT:
            operands = []  # COUNT(*) counts rows
        else:
            operands = [argument]
        if function is not ir.AggregateFunction.COUNT and len(operands) != 1:
            self.unsupported(f"{name} with {len(operands)} arguments", node)
            return None

        self.no_aggregates_in = "another aggregate function"
        arguments = [self.expression(operand) for operand in operands]
        self.no_aggregates_in = None
        if any(argument is None for argument in arguments):
            return None
        return ir.AggregateCall(function, tuple(arguments), distinct)  # type: ignore[arg-type]

    # --- Expressions -----------------------------------------------------

    def expression_without_aggregates(
        self, node: exp.Expression, context: str
    ) -> ir.Expression | None:
        """Translate an expression in a place where aggregates are not allowed."""
        previous = self.no_aggregates_in
        self.no_aggregates_in = context
        try:
            return self.expression(node)
        finally:
            self.no_aggregates_in = previous

    def expression(self, node: exp.Expression) -> ir.Expression | None:
        """Translate one expression, recursing into its operands."""
        if isinstance(node, exp.Paren):
            # Parentheses only shape the tree; the tree already encodes them.
            return self.expression(node.this)
        if isinstance(node, exp.Column):
            return self.column(node)
        if isinstance(node, exp.Literal | exp.Boolean | exp.Null):
            return self.literal(node)
        if isinstance(node, exp.Neg):
            return self.negation(node)
        if isinstance(node, exp.Not):
            return self.not_(node)
        handler = (
            self._PREDICATES_AND_CONDITIONALS.get(type(node))
            or self._FUNCTIONS.get(type(node))
            or self._DATE_FUNCTIONS.get(type(node))
        )
        if handler is not None:
            return handler(self, node)

        operator = _BINARY_OPERATORS.get(type(node))
        if operator is not None:
            return self.binary(node, operator)

        function = _AGGREGATE_FUNCTIONS.get(type(node))
        if function is not None:
            return self.aggregate_call(node, function)  # type: ignore[arg-type]

        if isinstance(node, exp.Func):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            self.unsupported(f"function {name.upper()}", node)
        else:
            self.unsupported(f"{node.key.upper()} expression", node)
        return None

    # --- Predicates, conditionals, and casts ------------------------------

    def not_(self, node: exp.Not) -> ir.Expression | None:
        inner = node.this.unnest()
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            # "x IS NOT NULL": isNotNull() gets the same column name as SQL.
            operand = self.expression(inner.this)
            return None if operand is None else ir.IsNull(operand, negated=True)
        operand = self.expression(node.this)
        return None if operand is None else ir.UnaryOp(ir.UnaryOperator.NOT, operand)

    def check_parts(self, node: exp.Expression, allowed: set[str], name: str) -> bool:
        """Reject any part of ``node`` outside ``allowed`` (fail closed)."""
        ok = True
        for part, value in node.args.items():
            if value and part not in allowed:
                label = _PART_LABELS.get(part, part.upper())
                self.unsupported(f"{name} with {label}", node)
                ok = False
        return ok

    def translate_all(
        self, nodes: list[exp.Expression]
    ) -> tuple[ir.Expression, ...] | None:
        """Translate every node, reporting all issues; None if any failed."""
        translated = [self.expression(node) for node in nodes]
        if any(item is None for item in translated):
            return None
        return tuple(item for item in translated if item is not None)

    def in_list(self, node: exp.In) -> ir.Expression | None:
        if not self.check_parts(node, {"this", "expressions"}, "IN"):
            return None
        operands = self.translate_all([node.this, *node.expressions])
        if operands is None:
            return None
        return ir.InList(operands[0], operands[1:])

    def between(self, node: exp.Between) -> ir.Expression | None:
        if not self.check_parts(node, {"this", "low", "high"}, "BETWEEN"):
            return None
        operands = self.translate_all([node.this, node.args["low"], node.args["high"]])
        return None if operands is None else ir.Between(*operands)

    def like(self, node: exp.Like | exp.ILike) -> ir.Expression | None:
        name = "ILIKE" if isinstance(node, exp.ILike) else "LIKE"
        if not self.check_parts(node, {"this", "expression", "negate"}, name):
            return None
        pattern = node.expression
        if not isinstance(pattern, exp.Literal) or not pattern.is_string:
            self.unsupported(f"{name} with a pattern that is not a constant", node)
            return None
        text = pattern.this
        if "\\" in text:
            self.unsupported(f"{name} pattern with a backslash", node, _BACKSLASH_HINT)
            return None
        if self.dialect == "tsql" and "[" in text:
            self.unsupported(f"{name} pattern with [ ]", node, _TSQL_BRACKETS_HINT)
            return None
        operand = self.expression(node.this)
        if operand is None:
            return None
        match = ir.Like(operand, text, case_insensitive=name == "ILIKE")
        if node.args.get("negate"):
            return ir.UnaryOp(ir.UnaryOperator.NOT, match)
        return match

    def escape(self, node: exp.Escape) -> None:
        self.unsupported("LIKE with ESCAPE", node, _BACKSLASH_HINT)

    def is_(self, node: exp.Is) -> ir.Expression | None:
        if not isinstance(node.expression, exp.Null):
            self.unsupported(f"IS {node.expression.sql(dialect=self.dialect)}", node)
            return None
        operand = self.expression(node.this)
        return None if operand is None else ir.IsNull(operand)

    def null_safe_equal(
        self, node: exp.NullSafeEQ | exp.NullSafeNEQ
    ) -> ir.Expression | None:
        operands = self.translate_all([node.this, node.expression])
        if operands is None:
            return None
        equal = ir.NullSafeEqual(*operands)
        if isinstance(node, exp.NullSafeNEQ):
            return ir.UnaryOp(ir.UnaryOperator.NOT, equal)
        return equal

    def case(self, node: exp.Case) -> ir.Expression | None:
        if not self.check_parts(node, {"this", "ifs", "default"}, "CASE"):
            return None
        subject_node = node.args.get("this")
        subject = self.expression(subject_node) if subject_node is not None else None
        failed = subject_node is not None and subject is None

        branches: list[tuple[ir.Expression, ir.Expression]] = []
        for branch in node.args["ifs"]:
            when, then = (
                self.expression(branch.this),
                self.expression(branch.args["true"]),
            )
            if when is None or then is None:
                failed = True
                continue
            if subject is not None:
                # Simple CASE compares with "=", so a NULL subject or a NULL
                # WHEN value never matches.
                when = ir.BinaryOp(ir.BinaryOperator.EQUAL, subject, when)
            branches.append((when, then))

        default_node = node.args.get("default")
        default = self.expression(default_node) if default_node is not None else None
        failed |= default_node is not None and default is None
        return None if failed else ir.Case(tuple(branches), default)

    def if_(self, node: exp.If) -> ir.Expression | None:
        """IF(condition, a, b) and T-SQL's IIF are a two-way CASE."""
        if not self.check_parts(node, {"this", "true", "false"}, "IF"):
            return None
        otherwise = node.args.get("false")
        operands = self.translate_all(
            [node.this, node.args["true"], *([otherwise] if otherwise else [])]
        )
        if operands is None:
            return None
        default = operands[2] if len(operands) == 3 else None
        return ir.Case(((operands[0], operands[1]),), default)

    def coalesce(self, node: exp.Coalesce) -> ir.Expression | None:
        if node.args.get("is_null"):
            self.unsupported("ISNULL", node, _TSQL_ISNULL_HINT)
            return None
        if not self.check_parts(node, {"this", "expressions", "is_nvl"}, "COALESCE"):
            return None
        operands = self.translate_all([node.this, *node.expressions])
        return None if operands is None else ir.FunctionCall("coalesce", operands)

    def nullif(self, node: exp.Nullif) -> ir.Expression | None:
        if not self.check_parts(node, {"this", "expression"}, "NULLIF"):
            return None
        operands = self.translate_all([node.this, node.expression])
        return None if operands is None else ir.FunctionCall("nullif", operands)

    def cast(self, node: exp.Cast) -> ir.Expression | None:
        safe = isinstance(node, exp.TryCast) or bool(node.args.get("safe"))
        name = "TRY_CAST" if safe else "CAST"
        if not self.check_parts(node, {"this", "to", "safe"}, name):
            return None
        data_type = self.spark_type(node.to, node, name)
        operand = self.expression(node.this)
        if data_type is None or operand is None:
            return None
        return ir.Cast(operand, data_type, safe)

    def spark_type(
        self, data_type: exp.DataType, node: exp.Expression, name: str
    ) -> str | None:
        """Map a SQL type to a Spark type, for types whose meaning SparkShift
        knows to be the same in every supported dialect."""
        kind = data_type.this
        parameters = data_type.expressions
        if kind in _SIMPLE_CAST_TYPES and not parameters:
            return _SIMPLE_CAST_TYPES[kind]
        if kind in _STRING_CAST_TYPES and not parameters:
            if self.dialect == "tsql" and kind is not exp.DataType.Type.TEXT:
                self.unsupported(
                    f"{name} to {data_type.sql(dialect=self.dialect)} without a length",
                    node,
                    _TSQL_VARCHAR_HINT,
                )
                return None
            return "string"
        if kind in _STRING_CAST_TYPES and _is_max_length(parameters):
            return "string"  # VARCHAR(MAX) is unbounded, like a Spark string
        if kind is exp.DataType.Type.DECIMAL and parameters:
            precision = int(parameters[0].name)
            scale = int(parameters[1].name) if len(parameters) > 1 else 0
            if 1 <= precision <= 38 and 0 <= scale <= precision:
                return f"decimal({precision},{scale})"
        if not parameters:
            timestamp_type = self.timestamp_type(kind)
            if timestamp_type is not None:
                return timestamp_type
        hint = _CAST_HINTS.get(kind)
        if kind is exp.DataType.Type.DECIMAL and not parameters:
            hint = _DECIMAL_DEFAULT_HINT
        if self.dialect == "tsql" and kind in _TSQL_IMPRECISE_DATETIMES:
            hint = _TSQL_DATETIME_HINT
        self.unsupported(f"{name} to {data_type.sql(dialect=self.dialect)}", node, hint)
        return None

    def timestamp_type(self, kind: exp.DataType.Type) -> str | None:
        """Map a timestamp type to Spark's point-in-time ``timestamp`` or its
        wall-clock ``timestamp_ntz``.

        SQLGlot already normalizes names per dialect: MySQL's and BigQuery's
        TIMESTAMP are TIMESTAMPTZ (points in time), BigQuery's DATETIME is
        TIMESTAMP (wall clock), and T-SQL's TIMESTAMP is ROWVERSION.
        """
        types = exp.DataType.Type
        if kind in (types.TIMESTAMPTZ, types.TIMESTAMPLTZ):
            return "timestamp"
        if kind is types.TIMESTAMPNTZ:
            return "timestamp_ntz"
        if kind is types.TIMESTAMP:
            # In generic SQL, TIMESTAMP means Spark's own TIMESTAMP; in the
            # supported dialects it is a wall-clock time.
            return "timestamp" if self.dialect is None else "timestamp_ntz"
        if (kind, self.dialect) in {
            (types.DATETIME, "mysql"),
            (types.DATETIME2, "tsql"),
        }:
            return "timestamp_ntz"
        return None

    _PREDICATES_AND_CONDITIONALS: ClassVar[
        dict[type[exp.Expression], Callable[..., ir.Expression | None]]
    ] = {
        exp.In: in_list,
        exp.Between: between,
        exp.Like: like,
        exp.ILike: like,
        exp.Escape: escape,
        exp.Is: is_,
        exp.NullSafeEQ: null_safe_equal,
        exp.NullSafeNEQ: null_safe_equal,
        exp.Case: case,
        exp.If: if_,
        exp.Coalesce: coalesce,
        exp.Nullif: nullif,
        exp.Cast: cast,
        exp.TryCast: cast,
    }

    # --- String and numeric functions --------------------------------------

    def simple_function(self, node: exp.Func) -> ir.Expression | None:
        """Functions with one argument and the same meaning in every dialect."""
        name = _SIMPLE_FUNCTIONS[type(node)]
        if not self.check_parts(node, {"this"}, node.sql_name()):
            return None
        operand = self.expression(node.this)
        return None if operand is None else ir.FunctionCall(name, (operand,))

    def length(self, node: exp.Length) -> ir.Expression | None:
        if not self.check_parts(node, {"this", "binary"}, "LENGTH"):
            return None
        operand = self.expression(_without_implicit_string_cast(node.this))
        if operand is None:
            return None
        if self.dialect == "tsql":
            # T-SQL's LEN ignores trailing spaces: LEN('a  ') is 1.
            trimmed = ir.FunctionCall("rtrim", (operand,))
            return ir.FunctionCall("length", (trimmed,))
        if self.dialect == "mysql" and node.args.get("binary"):
            # MySQL's LENGTH counts bytes; CHAR_LENGTH counts characters.
            return ir.FunctionCall("octet_length", (operand,))
        return ir.FunctionCall("length", (operand,))

    def trim(self, node: exp.Trim) -> ir.Expression | None:
        if not self.check_parts(node, {"this", "position"}, "TRIM"):
            return None
        if self.dialect == "bigquery":
            self.unsupported("TRIM", node, _BIGQUERY_TRIM_HINT)
            return None
        operand = self.expression(node.this)
        if operand is None:
            return None
        name = _TRIM_FUNCTIONS[node.args.get("position") or "BOTH"]
        return ir.FunctionCall(name, (operand,))

    def substring(self, node: exp.Substring) -> ir.Expression | None:
        allowed = {"this", "start", "length", "zero_start"}
        if not self.check_parts(node, allowed, "SUBSTRING"):
            return None
        start = self.constant_int(node.args.get("start"), node, "SUBSTRING start", 1)
        length_node = node.args.get("length")
        length = None
        if length_node is not None:
            length = self.constant_int(length_node, node, "SUBSTRING length", 0)
        operand = self.expression(node.this)
        if operand is None or start is None:
            return None
        if length_node is None:
            return ir.FunctionCall("substr", (operand, ir.Literal(start)))
        if length is None:
            return None
        arguments = (operand, ir.Literal(start), ir.Literal(length))
        return ir.FunctionCall("substring", arguments)

    def left_or_right(self, node: exp.Left | exp.Right) -> ir.Expression | None:
        name = "left" if isinstance(node, exp.Left) else "right"
        allowed = {"this", "expression", "negative_length_returns_empty"}
        if not self.check_parts(node, allowed, name.upper()):
            return None
        count = self.constant_int(node.expression, node, f"{name.upper()} length", 0)
        operand = self.expression(_without_implicit_string_cast(node.this))
        if operand is None or count is None:
            return None
        return ir.FunctionCall(name, (operand, ir.Literal(count)))

    def concat(self, node: exp.Concat | exp.DPipe) -> ir.Expression | None:
        if isinstance(node, exp.DPipe):
            allowed, parts = {"this", "expression", "safe"}, _flatten_dpipe(node)
        else:
            allowed = {"expressions", "safe", "coalesce"}
            parts = list(node.expressions)
        if not self.check_parts(node, allowed, "CONCAT"):
            return None
        operands = self.translate_all(parts)
        if operands is None:
            return None
        if node.args.get("coalesce") or self.dialect == "oracle":
            # PostgreSQL's and T-SQL's CONCAT, and Oracle's || and CONCAT, skip
            # NULL inputs; so does concat_ws. Spark's concat returns NULL.
            return ir.FunctionCall("concat_ws", (ir.Literal(""), *operands))
        return ir.FunctionCall("concat", operands)

    def replace_(self, node: exp.Replace) -> ir.Expression | None:
        if not self.check_parts(node, {"this", "expression", "replacement"}, "REPLACE"):
            return None
        operands = self.translate_all(
            [node.this, node.expression, node.args["replacement"]]
        )
        return None if operands is None else ir.FunctionCall("replace", operands)

    def round_(self, node: exp.Round) -> ir.Expression | None:
        allowed = {"this", "decimals", "truncate", "casts_non_integer_decimals"}
        if not self.check_parts(node, allowed, "ROUND"):
            return None
        if node.args.get("truncate"):
            self.unsupported("ROUND with a truncation argument", node)
            return None
        if self.dialect in _HALF_EVEN_FLOAT_ROUND_DIALECTS:
            self.unsupported("ROUND", node, _ROUND_HINT)
            return None
        decimals_node = node.args.get("decimals")
        decimals = None
        if decimals_node is not None:
            decimals = self.constant_int(decimals_node, node, "ROUND scale", None)
        operand = self.expression(node.this)
        if operand is None:
            return None
        if decimals_node is None:
            return ir.FunctionCall("round", (operand,))
        if decimals is None:
            return None
        return ir.FunctionCall("round", (operand, ir.Literal(decimals)))

    def pow_(self, node: exp.Pow) -> ir.Expression | None:
        if not self.check_parts(node, {"this", "expression"}, "POWER"):
            return None
        operands = self.translate_all([node.this, node.expression])
        return None if operands is None else ir.FunctionCall("pow", operands)

    def log(self, node: exp.Log) -> ir.Expression | None:
        if not self.check_parts(node, {"this", "expression"}, "LOG"):
            return None
        if node.expression is None:
            self.unsupported("LOG with one argument", node, _LOG_HINT)
            return None
        base = _number_literal(node.this)
        if base is None or base <= 0:
            self.unsupported("LOG with a base that is not a positive constant", node)
            return None
        operand = self.expression(node.expression)
        if operand is None:
            return None
        return ir.FunctionCall("log", (ir.Literal(float(base)), operand))

    def greatest_or_least(self, node: exp.Greatest | exp.Least) -> ir.Expression | None:
        name = "greatest" if isinstance(node, exp.Greatest) else "least"
        allowed = {"this", "expressions", "ignore_nulls"}
        if not self.check_parts(node, allowed, name.upper()):
            return None
        if not node.args.get("ignore_nulls") or self.dialect == "oracle":
            self.unsupported(name.upper(), node, _GREATEST_HINT)
            return None
        operands = self.translate_all([node.this, *node.expressions])
        return None if operands is None else ir.FunctionCall(name, operands)

    def constant_int(
        self,
        node: exp.Expression | None,
        owner: exp.Expression,
        what: str,
        minimum: int | None,
    ) -> int | None:
        """Read a constant integer argument, such as a SUBSTRING position."""
        value = _integer_literal(node)
        if value is None or (minimum is not None and value < minimum):
            requirement = (
                "a constant integer"
                if minimum is None
                else f"a constant integer of at least {minimum}"
            )
            self.unsupported(f"{what} that is not {requirement}", owner, _CONSTANT_HINT)
            return None
        return value

    _FUNCTIONS: ClassVar[
        dict[type[exp.Expression], Callable[..., ir.Expression | None]]
    ] = {
        exp.Upper: simple_function,
        exp.Lower: simple_function,
        exp.Abs: simple_function,
        exp.Ceil: simple_function,
        exp.Floor: simple_function,
        exp.Sqrt: simple_function,
        exp.Exp: simple_function,
        exp.Ln: simple_function,
        exp.Sign: simple_function,
        exp.Length: length,
        exp.Trim: trim,
        exp.Substring: substring,
        exp.Left: left_or_right,
        exp.Right: left_or_right,
        exp.Concat: concat,
        exp.DPipe: concat,
        exp.Replace: replace_,
        exp.Round: round_,
        exp.Pow: pow_,
        exp.Log: log,
        exp.Greatest: greatest_or_least,
        exp.Least: greatest_or_least,
    }

    # --- Dates and timestamps --------------------------------------------

    def extract(self, node: exp.Extract) -> ir.Expression | None:
        """EXTRACT(part FROM x), and T-SQL's DATEPART(part, x)."""
        if not self.check_parts(node, {"this", "expression"}, "EXTRACT"):
            return None
        part = node.this.name.upper()
        name = _DATE_PARTS.get(part)
        if name is None:
            self.unsupported(f"EXTRACT {part}", node, _DATE_PART_HINTS.get(part))
            return None
        operand = self.expression(node.expression)
        return None if operand is None else ir.FunctionCall(name, (operand,))

    def date_part_function(self, node: exp.Func) -> ir.Expression | None:
        """YEAR(x), MONTH(x), DAY(x), QUARTER(x), HOUR(x), DAYOFMONTH(x)."""
        if not self.check_parts(node, {"this"}, node.sql_name()):
            return None
        # T-SQL and MySQL wrap the argument in a CAST to DATE; the year,
        # month, and day of a timestamp are those of its date.
        argument = node.this
        if isinstance(argument, exp.TsOrDsToDate):
            argument = argument.this
        operand = self.expression(argument)
        if operand is None:
            return None
        return ir.FunctionCall(_DATE_PART_FUNCTIONS[type(node)], (operand,))

    def current_date_or_time(
        self, node: exp.CurrentDate | exp.CurrentTimestamp
    ) -> ir.Expression | None:
        if isinstance(node, exp.CurrentTimestamp) and node.args.get("sysdate"):
            self.unsupported("SYSDATE", node, _SYSDATE_HINT)
            return None
        name = (
            "current_date" if isinstance(node, exp.CurrentDate) else "current_timestamp"
        )
        if not self.check_parts(node, set(), name.upper()):
            return None
        return ir.FunctionCall(name, ())

    def date_diff(self, node: exp.DateDiff) -> ir.Expression | None:
        """Day differences: DATEDIFF(day, start, end), MySQL's DATEDIFF(end,
        start), and BigQuery's DATE_DIFF(end, start, DAY). SQLGlot puts the end
        in ``this`` and the start in ``expression`` for every dialect."""
        allowed = {"this", "expression", "unit", "date_part_boundary"}
        if not self.check_parts(node, allowed, "DATEDIFF"):
            return None
        if self.dialect in _NO_DATEDIFF_DIALECTS:
            self.unsupported("DATEDIFF", node, _NO_DATEDIFF_HINT)
            return None
        unit = node.args.get("unit")
        # Rendered rather than read by name: SQLGlot represents some units,
        # such as BigQuery's WEEK, as nodes without a plain name.
        unit_name = (
            unit.sql(dialect=self.dialect).upper() if unit is not None else "DAY"
        )
        if unit_name not in _KNOWN_DATE_UNITS:
            # A three-argument DATEDIFF in a dialect whose DATEDIFF takes two
            # arguments: SQLGlot reads a column name as the "unit".
            self.unsupported(
                "DATEDIFF in an unrecognized form", node, _DATEDIFF_FORM_HINT
            )
            return None
        if unit_name not in _DAY_UNITS:
            self.unsupported(f"DATEDIFF in {unit_name}", node, _DATEDIFF_UNIT_HINT)
            return None
        operands = self.translate_all(
            [
                _without_implicit_time_cast(node.this),
                _without_implicit_time_cast(node.expression),
            ]
        )
        return None if operands is None else ir.FunctionCall("datediff", operands)

    def date_add(self, node: exp.DateAdd | exp.DateSub) -> ir.Expression | None:
        """DATEADD(unit, n, x), DATE_ADD(x, INTERVAL n unit), DATE_SUB(...)."""
        name = "DATE_SUB" if isinstance(node, exp.DateSub) else "DATE_ADD"
        if not self.check_parts(node, {"this", "expression", "unit"}, name):
            return None
        amount_node, unit_node = node.expression, node.args.get("unit")
        if unit_node is None and not isinstance(amount_node, exp.Interval):
            return self.spark_date_add(node)
        if isinstance(amount_node, exp.Interval):
            amount_node, unit_node = amount_node.this, amount_node.args.get("unit")
        interval = self.interval(amount_node, unit_node, node)
        base = self.expression(node.this)
        if interval is None or base is None:
            return None
        operator = (
            ir.BinaryOperator.SUBTRACT
            if isinstance(node, exp.DateSub)
            else ir.BinaryOperator.ADD
        )
        return ir.BinaryOp(operator, base, interval)

    def spark_date_add(self, node: exp.DateAdd | exp.DateSub) -> ir.Expression | None:
        """Spark's own DATE_ADD(x, n) and DATE_SUB(x, n), which add whole days and
        always return a date. Only generic SQL means exactly this."""
        name = "date_sub" if isinstance(node, exp.DateSub) else "date_add"
        if self.dialect is not None:
            self.unsupported(f"{name.upper()} without a unit", node)
            return None
        operands = self.translate_all([node.this, node.expression])
        return None if operands is None else ir.FunctionCall(name, operands)

    def interval_arithmetic(
        self, node: exp.Add | exp.Sub, operator: ir.BinaryOperator
    ) -> ir.Expression | None:
        """x + INTERVAL 'n' unit, x - INTERVAL 'n' unit, INTERVAL 'n' unit + x."""
        left, right = node.this, node.expression
        if isinstance(left, exp.Interval) and isinstance(right, exp.Interval):
            self.unsupported("arithmetic on two intervals", node)
            return None
        if isinstance(left, exp.Interval) and isinstance(node, exp.Sub):
            self.unsupported("an interval minus a value", node)
            return None
        interval_node, base_node = (
            (left, right) if isinstance(left, exp.Interval) else (right, left)
        )
        interval = self.interval(
            interval_node.this, interval_node.args.get("unit"), interval_node
        )
        base = self.expression(base_node)
        if interval is None or base is None:
            return None
        return ir.BinaryOp(operator, base, interval)

    def interval(
        self,
        amount_node: exp.Expression | None,
        unit_node: exp.Expression | None,
        owner: exp.Expression,
    ) -> ir.Interval | None:
        unit_name = unit_node.name.upper() if unit_node is not None else ""
        unit = _INTERVAL_UNITS.get(unit_name)
        if unit is None:
            self.unsupported(
                f"date arithmetic in {unit_name or 'an unknown unit'}",
                owner,
                "Only DAY, WEEK, MONTH, and YEAR units are supported so far.",
            )
            return None
        if isinstance(amount_node, exp.Literal) and amount_node.is_string:
            # MySQL's INTERVAL '3' DAY and PostgreSQL's INTERVAL '3 days'.
            count = _integer_literal(exp.Literal.number(amount_node.this.strip()))
            if count is None:
                self.unsupported("interval amount that is not an integer", owner)
                return None
            return ir.Interval(unit, ir.Literal(count))
        amount = self.expression(amount_node) if amount_node is not None else None
        return None if amount is None else ir.Interval(unit, amount)

    def add_months(self, node: exp.AddMonths) -> ir.Expression | None:
        if self.dialect is not None or node.args.get("preserve_end_of_month"):
            self.unsupported("ADD_MONTHS", node, _ADD_MONTHS_HINT)
            return None
        if not self.check_parts(node, {"this", "expression"}, "ADD_MONTHS"):
            return None
        operands = self.translate_all([node.this, node.expression])
        return None if operands is None else ir.FunctionCall("add_months", operands)

    def date_trunc(
        self, node: exp.DateTrunc | exp.TimestampTrunc
    ) -> ir.Expression | None:
        """Truncation, whose result type differs by dialect: PostgreSQL and Spark
        return a timestamp; BigQuery's DATE_TRUNC returns a date."""
        allowed = {"this", "unit", "input_type_preserved"}
        if not self.check_parts(node, allowed, "DATE_TRUNC"):
            return None
        returns_timestamp = (
            self.dialect is None and isinstance(node, exp.DateTrunc)
        ) or (self.dialect == "postgres" and isinstance(node, exp.TimestampTrunc))
        returns_date = self.dialect == "bigquery" and isinstance(node, exp.DateTrunc)
        if node.args.get("input_type_preserved") or not (
            returns_timestamp or returns_date
        ):
            self.unsupported("DATE_TRUNC", node, _DATE_TRUNC_HINT)
            return None
        unit = node.args["unit"].name.upper()
        if unit not in _TRUNC_UNITS:
            self.unsupported(
                f"DATE_TRUNC to {unit}",
                node,
                "Only YEAR, QUARTER, and MONTH are supported so far.",
            )
            return None
        operand = self.expression(node.this)
        if operand is None:
            return None
        if returns_date:
            return ir.FunctionCall("trunc", (operand, ir.Literal(unit.lower())))
        return ir.FunctionCall("date_trunc", (ir.Literal(unit.lower()), operand))

    _DATE_FUNCTIONS: ClassVar[
        dict[type[exp.Expression], Callable[..., ir.Expression | None]]
    ] = {
        exp.Extract: extract,
        exp.Year: date_part_function,
        exp.Quarter: date_part_function,
        exp.Month: date_part_function,
        exp.Day: date_part_function,
        exp.DayOfMonth: date_part_function,
        exp.Hour: date_part_function,
        exp.CurrentDate: current_date_or_time,
        exp.CurrentTimestamp: current_date_or_time,
        exp.DateDiff: date_diff,
        exp.DateAdd: date_add,
        exp.DateSub: date_add,
        exp.AddMonths: add_months,
        exp.DateTrunc: date_trunc,
        exp.TimestampTrunc: date_trunc,
    }

    def column(self, node: exp.Column) -> ir.Column | None:
        if isinstance(node.this, exp.Star):
            # Only valid directly in the SELECT list, handled there.
            self.unsupported("qualified star", node)
            return None
        if node.args.get("join_mark"):
            # Oracle's "t2.id(+)" makes the join an outer join. SQLGlot keeps it
            # only as a flag on the column, so it must be checked explicitly.
            self.unsupported(
                "Oracle (+) outer join marker", node, hint=_ORACLE_OUTER_JOIN_HINT
            )
            return None
        if self.dialect == "oracle" and self.is_oracle_pseudo_column(node):
            # SQLGlot parses ROWNUM as an ordinary column; Spark has no such column.
            self.unsupported(
                f"{node.name.upper()} pseudo-column",
                node,
                hint="Use FETCH FIRST n ROWS ONLY to limit rows.",
            )
            return None
        return ir.Column(tuple(identifier.name for identifier in node.parts))

    @staticmethod
    def is_oracle_pseudo_column(node: exp.Column) -> bool:
        identifier = node.this
        return (
            not node.table
            and isinstance(identifier, exp.Identifier)
            and not identifier.quoted
            and identifier.name.upper() in {"ROWNUM", "ROWID"}
        )

    def literal(self, node: exp.Literal | exp.Boolean | exp.Null) -> ir.Literal:
        if isinstance(node, exp.Null):
            return ir.Literal(None)
        if isinstance(node, exp.Boolean):
            return ir.Literal(bool(node.this))
        if node.is_string:
            return ir.Literal(node.this)
        return ir.Literal(_number(node.this))

    def negation(self, node: exp.Neg) -> ir.Expression | None:
        operand = self.expression(node.this)
        if operand is None:
            return None
        # "-5" is a literal in Spark SQL; keep it one so types and names match.
        if isinstance(operand, ir.Literal) and type(operand.value) in (
            int,
            Decimal,
            float,
        ):
            return ir.Literal(-operand.value)  # type: ignore[operator]
        return ir.UnaryOp(ir.UnaryOperator.NEGATE, operand)

    def binary(
        self, node: exp.Expression, operator: ir.BinaryOperator
    ) -> ir.Expression | None:
        if isinstance(node, exp.Add | exp.Sub) and (
            isinstance(node.expression, exp.Interval)
            or isinstance(node.this, exp.Interval)
        ):
            return self.interval_arithmetic(node, operator)
        hint = self.dialect_semantics_hint(node)
        if hint is not None:
            name = "+ operator" if isinstance(node, exp.Add) else "division"
            self.unsupported(name, node, hint=hint)

        # Translate both sides even after an issue, so their issues are reported too.
        left = self.expression(node.this)
        right = self.expression(node.args["expression"])
        if hint is not None or left is None or right is None:
            return None
        return ir.BinaryOp(operator, left, right)

    def dialect_semantics_hint(self, node: exp.Expression) -> str | None:
        """Explain why an operator's meaning in the source dialect differs from
        Spark's, or return None if it means the same."""
        if isinstance(node, exp.Div) and node.args.get("typed"):
            return _INTEGER_DIVISION_HINT
        if isinstance(node, exp.Div) and node.args.get("safe"):
            return _SAFE_DIVISION_HINT
        if isinstance(node, exp.Add) and self.dialect == "tsql":
            return _TSQL_PLUS_HINT
        return None

    # --- Diagnostics -----------------------------------------------------

    def unsupported(self, message: str, node: object, hint: str | None = None) -> None:
        self.issues.append(Diagnostic(message, self.fragment(node), hint))

    def fragment(self, value: object) -> str:
        if isinstance(value, exp.Expression):
            return value.sql(dialect=self.dialect)
        return str(value)


def _number(text: str) -> int | Decimal | float:
    """Return the Python value matching Spark SQL's type for a numeric literal.

    Spark SQL reads ``30`` as an integer, ``1.50`` as an exact decimal,
    ``1.5e0`` as a double, and integers too large for 64 bits as decimals.
    """
    if "e" in text.lower():
        return float(text)
    if "." in text:
        return Decimal(text)
    value = int(text)
    if not _INT64_MIN <= value <= _INT64_MAX:
        return Decimal(text)
    return value


_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


def _is_aggregate_query(select: exp.Select) -> bool:
    """A query aggregates if it groups, filters groups, or selects an aggregate."""
    if select.args.get("group") or select.args.get("having"):
        return True
    for node in select.expressions:
        for aggregate in node.find_all(exp.AggFunc):
            # Aggregates inside a window function or a subquery do not make
            # this query an aggregation.
            if aggregate.find_ancestor(exp.Window, exp.Select) is select:
                return True
    return False


def _calls_function(expression: ir.Expression) -> bool:
    """Whether a scalar function call (including an interval, which becomes
    make_interval) appears anywhere in an expression."""
    if isinstance(expression, ir.FunctionCall | ir.Interval):
        return True
    return any(_calls_function(child) for child in ir.children(expression))


def _without_implicit_time_cast(node: exp.Expression) -> exp.Expression:
    """Remove the string-to-timestamp conversion SQLGlot inserts around T-SQL
    DATEDIFF arguments; Spark's datediff accepts dates and timestamps."""
    if isinstance(node, exp.TimeStrToTime):
        return node.this
    return node


def _is_max_length(parameters: list[exp.Expression]) -> bool:
    return len(parameters) == 1 and parameters[0].name.upper() == "MAX"


def _without_implicit_string_cast(node: exp.Expression) -> exp.Expression:
    """Remove the cast to an unbounded string that SQLGlot inserts around T-SQL
    string function arguments (it represents VARCHAR(MAX) as TEXT); Spark
    converts those arguments to strings itself."""
    if (
        isinstance(node, exp.Cast)
        and node.to.this in _STRING_CAST_TYPES
        and (not node.to.expressions or _is_max_length(node.to.expressions))
    ):
        return node.this
    return node


def _flatten_dpipe(node: exp.Expression) -> list[exp.Expression]:
    """Turn a || b || c into [a, b, c]."""
    if isinstance(node, exp.DPipe):
        return [*_flatten_dpipe(node.this), *_flatten_dpipe(node.expression)]
    return [node]


def _integer_literal(node: exp.Expression | None) -> int | None:
    if isinstance(node, exp.Neg):
        value = _integer_literal(node.this)
        return None if value is None else -value
    if isinstance(node, exp.Literal) and not node.is_string and node.this.isdigit():
        return int(node.this)
    return None


def _number_literal(node: exp.Expression | None) -> float | None:
    if isinstance(node, exp.Literal) and not node.is_string:
        return float(node.this)
    return None


def _differently_named(node: exp.Expression) -> str | None:
    """Name constructs whose unaliased output column Spark SQL and PySpark
    name differently: SQL says ``between(a, 1, 5)``, PySpark
    ``((a >= 1) AND (a <= 5))``."""
    inner = node.unnest()
    if isinstance(inner, exp.Not):
        inner = inner.this.unnest()
    if isinstance(inner, exp.Between):
        return "BETWEEN"
    if isinstance(inner, exp.If):
        return "IF"
    return None


def _plain_column(item: ir.Expression) -> tuple[ir.Column, str] | None:
    """Return the column and output name of ``col`` or ``col AS name``."""
    if isinstance(item, ir.Column):
        return item, item.name_parts[-1]
    if isinstance(item, ir.Alias) and isinstance(item.expression, ir.Column):
        return item.expression, item.name
    return None


def _matching_key(column: ir.Column, keys: list[ir.Column]) -> ir.Column | None:
    """Find the GROUP BY key a column refers to.

    ``country`` and ``c.country`` refer to the same key when one of them is
    unqualified, as SQL name resolution allows.
    """
    for key in keys:
        if column.name_parts == key.name_parts:
            return key
        unqualified = len(column.name_parts) == 1 or len(key.name_parts) == 1
        if unqualified and column.name_parts[-1] == key.name_parts[-1]:
            return key
    return None


def _free_columns(expression: ir.Expression) -> Iterator[ir.Column]:
    """Yield the columns an expression uses outside of aggregate calls."""
    if isinstance(expression, ir.Column):
        yield expression
    elif not isinstance(expression, ir.AggregateCall):
        for child in ir.children(expression):
            yield from _free_columns(child)


def _rewrite(
    expression: ir.Expression,
    replace: Callable[[ir.Expression], ir.Expression | None],
) -> ir.Expression:
    """Rebuild an expression, substituting nodes for which ``replace`` returns
    a value; the children of a substituted node are not visited."""
    replacement = replace(expression)
    if replacement is not None:
        return replacement
    return ir.map_children(expression, lambda child: _rewrite(child, replace))
