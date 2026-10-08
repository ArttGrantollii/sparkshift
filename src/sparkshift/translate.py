"""Translate a SQLGlot syntax tree into SparkShift's IR.

The translator decides what SparkShift can convert. It works from an
allowlist: it handles the parts of a query it understands and reports every
other part that is present as unsupported — including parts added by future
SQLGlot versions — so nothing is ever silently ignored. All unsupported parts
are collected before failing, so users see them at once.
"""

from decimal import Decimal

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
        distinct = False
        limit: int | None = None
        failed = False

        # Walk the parts in SQLGlot's declared order so diagnostics are
        # deterministic.
        for part in exp.Select.arg_types:
            value = select.args.get(part)
            if part == "expressions":
                items = self.projection(value)
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
        # clauses are written in: FROM, WHERE, SELECT, DISTINCT, LIMIT.
        relation = source
        if condition is not None:
            relation = ir.Filter(relation, condition)
        if items != (ir.Star(),):
            relation = ir.Project(relation, items)
        if distinct:
            relation = ir.Distinct(relation)
        if limit is not None:
            relation = ir.Limit(relation, limit)
        return relation

    def where(self, where: exp.Where, select: exp.Select) -> ir.Expression | None:
        issues_before = len(self.issues)
        if self.dialect == "snowflake":
            self.check_alias_references(where, select)
        condition = self.expression(where.this)
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
        condition = self.expression(on) if on is not None else None
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
        return expression

    # --- Expressions -----------------------------------------------------

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
            operand = self.expression(node.this)
            return (
                None if operand is None else ir.UnaryOp(ir.UnaryOperator.NOT, operand)
            )

        operator = _BINARY_OPERATORS.get(type(node))
        if operator is not None:
            return self.binary(node, operator)

        if isinstance(node, exp.Func):
            name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
            self.unsupported(f"function {name.upper()}", node)
        else:
            self.unsupported(f"{node.key.upper()} expression", node)
        return None

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
