from decimal import Decimal

import pytest
from sqlglot import exp

from sparkshift import ir
from sparkshift.errors import SQLParseError, UnsupportedSQLError
from sparkshift.ir import (
    Alias,
    BinaryOp,
    BinaryOperator,
    Column,
    Literal,
    Project,
    Star,
    TableScan,
    UnaryOp,
    UnaryOperator,
)
from sparkshift.parsing import parse_sql
from sparkshift.translate import translate


def translate_sql(sql: str, dialect: str | None = None) -> ir.Relation:
    return translate(parse_sql(sql, dialect), dialect)


def select_items(sql: str, dialect: str | None = None) -> tuple[ir.Expression, ...]:
    plan = translate_sql(sql, dialect)
    assert isinstance(plan, Project)
    return plan.items


def only_item(sql: str, dialect: str | None = None) -> ir.Expression:
    [item] = select_items(sql, dialect)
    return item


def unsupported_issues(sql: str, dialect: str | None = None) -> list[tuple[str, str]]:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql(sql, dialect)
    return [(issue.message, issue.sql) for issue in caught.value.issues]


A, B, C = Column(("a",)), Column(("b",)), Column(("c",))


# --- Tables ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "name_parts"),
    [
        ("SELECT * FROM customers", ("customers",)),
        ("SELECT * FROM sales.customers", ("sales", "customers")),
        ("SELECT * FROM main.sales.customers", ("main", "sales", "customers")),
        ('SELECT * FROM "my table"', ("my table",)),
    ],
)
def test_select_star_from_a_table_becomes_a_table_scan(
    sql: str, name_parts: tuple[str, ...]
) -> None:
    assert translate_sql(sql) == TableScan(name_parts)


def test_dialect_specific_table_names_are_split_correctly() -> None:
    plan = translate_sql("SELECT * FROM `proj.dataset.customers`", "bigquery")

    assert plan == TableScan(("proj", "dataset", "customers"))


# --- Projection --------------------------------------------------------------


def test_select_list_becomes_a_projection() -> None:
    plan = translate_sql("SELECT name, customers.country FROM customers")

    assert plan == Project(
        TableScan(("customers",)),
        (Column(("name",)), Column(("customers", "country"))),
    )


def test_alias_names_the_output_column() -> None:
    assert only_item("SELECT amount * 2 AS doubled FROM orders") == Alias(
        BinaryOp(BinaryOperator.MULTIPLY, Column(("amount",)), Literal(2)), "doubled"
    )


def test_star_can_be_combined_with_expressions() -> None:
    assert select_items("SELECT *, a FROM t") == (Star(), A)


def test_quoted_column_names_keep_their_text() -> None:
    assert only_item('SELECT "order id" FROM t') == Column(("order id",))


# --- Literals ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql_literal", "value"),
    [
        ("30", 30),
        ("3000000000", 3000000000),
        ("9223372036854775808", Decimal("9223372036854775808")),  # beyond 64 bits
        ("1.50", Decimal("1.50")),  # exact decimal, scale kept
        ("1.5e0", 1.5),  # double
        ("-5", -5),  # a negative literal, not a negation
        ("-1.5", Decimal("-1.5")),
        ("'US'", "US"),
        ("''", ""),
        ("TRUE", True),
        ("FALSE", False),
        ("NULL", None),
    ],
)
def test_literals_keep_spark_sql_types(
    sql_literal: str, value: ir.LiteralValue
) -> None:
    item = only_item(f"SELECT {sql_literal} AS x FROM t")

    assert item == Alias(Literal(value), "x")
    assert type(item.expression.value) is type(value)  # type: ignore[union-attr]


# --- Operators ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql_operator", "operator"),
    [
        ("+", BinaryOperator.ADD),
        ("-", BinaryOperator.SUBTRACT),
        ("*", BinaryOperator.MULTIPLY),
        ("/", BinaryOperator.DIVIDE),
        ("%", BinaryOperator.MODULO),
        ("=", BinaryOperator.EQUAL),
        ("<>", BinaryOperator.NOT_EQUAL),
        ("!=", BinaryOperator.NOT_EQUAL),
        ("<", BinaryOperator.LESS),
        ("<=", BinaryOperator.LESS_EQUAL),
        (">", BinaryOperator.GREATER),
        (">=", BinaryOperator.GREATER_EQUAL),
        ("AND", BinaryOperator.AND),
        ("OR", BinaryOperator.OR),
    ],
)
def test_binary_operators(sql_operator: str, operator: BinaryOperator) -> None:
    assert only_item(f"SELECT a {sql_operator} b FROM t") == BinaryOp(operator, A, B)


def test_not_and_negation() -> None:
    items = select_items("SELECT NOT a, -a AS negative_a FROM t")

    assert items == (
        UnaryOp(UnaryOperator.NOT, A),
        Alias(UnaryOp(UnaryOperator.NEGATE, A), "negative_a"),
    )


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "a + b * c",
            BinaryOp(BinaryOperator.ADD, A, BinaryOp(BinaryOperator.MULTIPLY, B, C)),
        ),
        (
            "(a + b) * c",
            BinaryOp(BinaryOperator.MULTIPLY, BinaryOp(BinaryOperator.ADD, A, B), C),
        ),
        (
            "a - b - c",
            BinaryOp(
                BinaryOperator.SUBTRACT, BinaryOp(BinaryOperator.SUBTRACT, A, B), C
            ),
        ),
        (
            "NOT a = 1 AND b",
            BinaryOp(
                BinaryOperator.AND,
                UnaryOp(
                    UnaryOperator.NOT, BinaryOp(BinaryOperator.EQUAL, A, Literal(1))
                ),
                B,
            ),
        ),
        (
            "a OR b AND c",
            BinaryOp(BinaryOperator.OR, A, BinaryOp(BinaryOperator.AND, B, C)),
        ),
    ],
)
def test_sql_precedence_is_captured_in_the_tree(
    sql: str, expected: ir.Expression
) -> None:
    assert only_item(f"SELECT {sql} FROM t") == expected


# --- Dialect semantics -------------------------------------------------------


@pytest.mark.parametrize("dialect", [None, "snowflake", "bigquery", "oracle"])
def test_division_with_spark_compatible_semantics_is_supported(
    dialect: str | None,
) -> None:
    assert only_item("SELECT a / b FROM t", dialect) == BinaryOp(
        BinaryOperator.DIVIDE, A, B
    )


@pytest.mark.parametrize(
    ("dialect", "sql", "message", "hint_fragment"),
    [
        ("tsql", "SELECT a / b FROM t", "division", "discards the remainder"),
        ("postgres", "SELECT a / b FROM t", "division", "discards the remainder"),
        ("mysql", "SELECT a / b FROM t", "division", "Division by zero returns NULL"),
        ("tsql", "SELECT a + b FROM t", "+ operator", "string concatenation"),
    ],
)
def test_operators_whose_meaning_differs_from_spark_are_rejected(
    dialect: str, sql: str, message: str, hint_fragment: str
) -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql(sql, dialect)

    [issue] = caught.value.issues
    assert issue.message == message
    assert issue.hint is not None
    assert hint_fragment in issue.hint


def test_tsql_subtraction_is_supported() -> None:
    assert only_item("SELECT a - b FROM t", "tsql") == BinaryOp(
        BinaryOperator.SUBTRACT, A, B
    )


# --- Unsupported: every construct is reported --------------------------------


def test_all_unsupported_constructs_are_reported_together() -> None:
    issues = unsupported_issues(
        "WITH RECURSIVE x AS (SELECT a FROM t) SELECT name FROM customers "
        "LIMIT 5 OFFSET 2"
    )

    assert issues == [
        ("WITH RECURSIVE", "WITH RECURSIVE x AS (SELECT a FROM t)"),
        ("OFFSET clause", "OFFSET 2"),
    ]


def test_issues_inside_one_expression_are_all_reported() -> None:
    issues = unsupported_issues("SELECT my_udf(a) + other_udf(b) AS x FROM t")

    assert issues == [
        ("function MY_UDF", "MY_UDF(a)"),
        ("function OTHER_UDF", "OTHER_UDF(b)"),
    ]


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        ("SELECT -amount FROM t", None, ("negation without an alias", "-amount")),
        ("SELECT -my_udf(a) AS x FROM t", None, ("function MY_UDF", "MY_UDF(a)")),
        ("SELECT COUNT(t.*) AS n FROM t", None, ("qualified star", "t.*")),
        (
            "SELECT CAST(a AS FLOAT) AS x FROM t",
            None,
            ("CAST to FLOAT", "CAST(a AS FLOAT)"),
        ),
        ("SELECT a IS TRUE AS x FROM t", None, ("IS TRUE", "a IS TRUE")),
        (
            "SELECT a LIKE 'x!%' ESCAPE '!' AS x FROM t",
            None,
            ("LIKE with ESCAPE", "a LIKE 'x!%' ESCAPE '!'"),
        ),
        (
            "SELECT CONCAT_WS('-', a, b) AS x FROM t",
            None,
            ("function CONCAT_WS", "CONCAT_WS('-', a, b)"),
        ),
        ("SELECT * FROM t GROUP BY a", None, ("SELECT * with aggregation", "*")),
        ("SELECT * FROM t LIMIT 5 OFFSET 2", None, ("OFFSET clause", "OFFSET 2")),
        ("SELECT AS STRUCT * FROM t", "bigquery", ("SELECT AS", "STRUCT")),
        (
            "WITH x AS (SELECT 1) SELECT * FROM x",
            None,
            ("SELECT without FROM", "SELECT 1"),
        ),
        ("SELECT * FROM t WITH (NOLOCK)", "tsql", ("table hint", "t WITH (NOLOCK)")),
        (
            "SELECT * FROM t TABLESAMPLE (10 PERCENT)",
            None,
            ("TABLESAMPLE", "t TABLESAMPLE (10 PERCENT)"),
        ),
        (
            "SELECT * FROM (SELECT 1) s",
            None,
            ("SELECT without FROM", "SELECT 1"),
        ),
        (
            "SELECT * FROM UNNEST([1, 2])",
            "bigquery",
            ("FROM source other than a table", "UNNEST([1, 2])"),
        ),
        ("SELECT 1", None, ("SELECT without FROM", "SELECT 1")),
        (
            "SELECT * FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1",
            "snowflake",
            ("QUALIFY clause", "QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1"),
        ),
    ],
)
def test_unsupported_construct_is_reported(
    sql: str, dialect: str | None, expected: tuple[str, str]
) -> None:
    assert expected in unsupported_issues(sql, dialect)


def test_each_unsupported_join_is_reported_separately() -> None:
    issues = unsupported_issues("SELECT * FROM a NATURAL JOIN b NATURAL JOIN c")

    assert issues == [
        ("NATURAL JOIN", "NATURAL JOIN b"),
        ("NATURAL JOIN", "NATURAL JOIN c"),
    ]


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("INSERT INTO t VALUES (1)", "INSERT statement"),
        ("UPDATE t SET a = 1", "UPDATE statement"),
        ("DELETE FROM t", "DELETE statement"),
        ("CREATE TABLE t (a INT)", "CREATE statement"),
        ("SELECT * FROM a UNION SELECT * FROM b", "UNION statement"),
    ],
)
def test_non_select_statements_are_rejected_with_a_hint(sql: str, message: str) -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql(sql)

    [issue] = caught.value.issues
    assert issue.message == message
    assert issue.hint == "Only SELECT queries can be converted."


def test_select_without_columns_is_a_parse_error() -> None:
    # The parser accepts this (see test_parsing); the translator must not.
    with pytest.raises(SQLParseError, match="SELECT has no columns"):
        translate_sql("SELECT FROM t")


# --- WHERE, DISTINCT, LIMIT --------------------------------------------------

T = TableScan(("t",))


def test_clauses_are_built_in_evaluation_order() -> None:
    # Written SELECT ... WHERE ... LIMIT, but evaluated FROM, WHERE, SELECT,
    # DISTINCT, LIMIT.
    plan = translate_sql("SELECT DISTINCT a FROM t WHERE b > 1 LIMIT 3")

    assert plan == ir.Limit(
        ir.Distinct(
            Project(
                ir.Filter(T, BinaryOp(BinaryOperator.GREATER, B, Literal(1))),
                (A,),
            )
        ),
        3,
    )


def test_where_with_select_star_filters_without_projecting() -> None:
    assert translate_sql("SELECT * FROM t WHERE a") == ir.Filter(T, A)


def test_select_all_is_not_distinct() -> None:
    assert translate_sql("SELECT ALL a FROM t") == Project(T, (A,))


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT * FROM t LIMIT 5", None),
        ("SELECT TOP 5 * FROM t", "tsql"),
        ("SELECT TOP (5) * FROM t", "tsql"),
        ("SELECT * FROM t FETCH FIRST 5 ROWS ONLY", "oracle"),
    ],
)
def test_row_limits_in_every_dialect_spelling(sql: str, dialect: str | None) -> None:
    assert translate_sql(sql, dialect) == ir.Limit(T, 5)


def test_limit_zero_is_allowed() -> None:
    assert translate_sql("SELECT * FROM t LIMIT 0") == ir.Limit(T, 0)


@pytest.mark.parametrize(
    ("sql", "dialect", "message"),
    [
        ("SELECT TOP 5 PERCENT * FROM t", "tsql", "row limit in PERCENT"),
        ("SELECT TOP 5 WITH TIES * FROM t ORDER BY a", "tsql", "row limit WITH TIES"),
        (
            "SELECT * FROM t FETCH FIRST 5 PERCENT ROWS ONLY",
            "oracle",
            "row limit in PERCENT",
        ),
        (
            "SELECT * FROM t LIMIT a",
            None,
            "row limit that is not a non-negative integer",
        ),
        (
            "SELECT * FROM t LIMIT -1",
            None,
            "row limit that is not a non-negative integer",
        ),
        (
            "SELECT * FROM t LIMIT 2 + 3",
            None,
            "row limit that is not a non-negative integer",
        ),
        ("SELECT DISTINCT ON (a) a, b FROM t", "postgres", "DISTINCT ON"),
        ("SELECT * FROM t WHERE ROWNUM <= 5", "oracle", "ROWNUM pseudo-column"),
        ("SELECT ROWID AS r FROM t", "oracle", "ROWID pseudo-column"),
        (
            "SELECT amount * 2 AS doubled FROM t WHERE doubled > 100",
            "snowflake",
            "WHERE reference to a SELECT alias",
        ),
        (
            "SELECT * FROM t WHERE COUNT(a) > 1",
            None,
            "aggregate function COUNT in WHERE",
        ),
    ],
)
def test_unsupported_filters_and_limits(
    sql: str, dialect: str | None, message: str
) -> None:
    messages = [issue for issue, _ in unsupported_issues(sql, dialect)]

    assert message in messages


def test_rownum_is_an_ordinary_column_outside_oracle() -> None:
    assert translate_sql("SELECT rownum FROM t") == Project(T, (Column(("rownum",)),))


def test_snowflake_where_may_use_a_qualified_column_named_like_an_alias() -> None:
    plan = translate_sql("SELECT a AS b FROM t WHERE t.b > 1", "snowflake")

    assert isinstance(plan, Project)
    assert plan.source == ir.Filter(
        T, BinaryOp(BinaryOperator.GREATER, Column(("t", "b")), Literal(1))
    )


def test_mysql_offset_comma_form_is_rejected() -> None:
    assert ("OFFSET clause", "OFFSET 2") in unsupported_issues(
        "SELECT * FROM t LIMIT 2, 5", "mysql"
    )


def test_unknown_parts_of_a_row_limit_are_rejected_not_ignored() -> None:
    # Simulates a slot that none of our dialects produce today, such as one a
    # future SQLGlot version might add: the allowlist must fail closed.
    from sqlglot import exp

    tree = parse_sql("SELECT * FROM t LIMIT 5")
    tree.args["limit"].set("offset", exp.Literal.number(2))

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    assert [issue.message for issue in caught.value.issues] == ["row limit OFFSET"]


# --- Table aliases and joins -------------------------------------------------

CUSTOMERS = TableScan(("customers",))
ORDERS = TableScan(("orders",))
CUSTOMERS_C = ir.RelationAlias(CUSTOMERS, "c")
ORDERS_O = ir.RelationAlias(ORDERS, "o")
KEYS_MATCH = BinaryOp(
    BinaryOperator.EQUAL, Column(("c", "customer_id")), Column(("o", "customer_id"))
)


def test_single_table_alias() -> None:
    plan = translate_sql("SELECT c.name FROM customers c")

    assert plan == Project(CUSTOMERS_C, (Column(("c", "name")),))


def test_qualified_star() -> None:
    assert select_items("SELECT c.*, o.amount FROM customers c, orders o") == (
        Star(("c",)),
        Column(("o", "amount")),
    )


@pytest.mark.parametrize(
    ("join_sql", "kind"),
    [
        ("JOIN", ir.JoinKind.INNER),
        ("INNER JOIN", ir.JoinKind.INNER),
        ("LEFT JOIN", ir.JoinKind.LEFT),
        ("LEFT OUTER JOIN", ir.JoinKind.LEFT),
        ("RIGHT JOIN", ir.JoinKind.RIGHT),
        ("RIGHT OUTER JOIN", ir.JoinKind.RIGHT),
        ("FULL JOIN", ir.JoinKind.FULL),
        ("FULL OUTER JOIN", ir.JoinKind.FULL),
    ],
)
def test_join_kinds(join_sql: str, kind: ir.JoinKind) -> None:
    plan = translate_sql(
        f"SELECT * FROM customers c {join_sql} orders o "
        "ON c.customer_id = o.customer_id"
    )

    assert plan == ir.Join(CUSTOMERS_C, ORDERS_O, kind, KEYS_MATCH)


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT * FROM customers c CROSS JOIN orders o", None),
        ("SELECT * FROM customers c, orders o", None),
        # MySQL treats a JOIN without ON as a cross join; SQLGlot represents it
        # exactly like the comma form.
        ("SELECT * FROM customers c JOIN orders o", "mysql"),
    ],
)
def test_cross_joins(sql: str, dialect: str | None) -> None:
    assert translate_sql(sql, dialect) == ir.Join(
        CUSTOMERS_C, ORDERS_O, ir.JoinKind.CROSS
    )


def test_join_using() -> None:
    plan = translate_sql("SELECT * FROM customers JOIN orders USING (customer_id, x)")

    assert plan == ir.Join(
        ir.RelationAlias(CUSTOMERS, "customers"),
        ir.RelationAlias(ORDERS, "orders"),
        ir.JoinKind.INNER,
        using=("customer_id", "x"),
    )


def test_joined_tables_without_aliases_are_named_after_the_table() -> None:
    plan = translate_sql(
        "SELECT * FROM sales.customers JOIN orders ON customers.id = orders.id"
    )

    assert isinstance(plan, ir.Join)
    assert plan.left == ir.RelationAlias(TableScan(("sales", "customers")), "customers")
    assert plan.right == ir.RelationAlias(ORDERS, "orders")


def test_joins_associate_left_to_right() -> None:
    plan = translate_sql(
        "SELECT * FROM a JOIN b ON a.id = b.id LEFT JOIN c ON b.id = c.id"
    )

    assert isinstance(plan, ir.Join)
    assert plan.kind is ir.JoinKind.LEFT
    assert isinstance(plan.left, ir.Join)
    assert plan.left.kind is ir.JoinKind.INNER


def test_join_then_where_then_select() -> None:
    plan = translate_sql(
        "SELECT o.amount FROM customers c "
        "JOIN orders o ON c.customer_id = o.customer_id WHERE o.amount > 0"
    )

    assert plan == Project(
        ir.Filter(
            ir.Join(CUSTOMERS_C, ORDERS_O, ir.JoinKind.INNER, KEYS_MATCH),
            BinaryOp(BinaryOperator.GREATER, Column(("o", "amount")), Literal(0)),
        ),
        (Column(("o", "amount")),),
    )


@pytest.mark.parametrize(
    ("sql", "dialect", "message"),
    [
        ("SELECT * FROM a NATURAL JOIN b", None, "NATURAL JOIN"),
        (
            "SELECT * FROM a ASOF JOIN b MATCH_CONDITION(a.t >= b.t) ON a.id = b.id",
            "snowflake",
            "ASOF JOIN",
        ),
        # SQLGlot accepts these in every supported dialect, so they must be rejected.
        ("SELECT * FROM a LEFT SEMI JOIN b ON a.id = b.id", None, "LEFT SEMI JOIN"),
        ("SELECT * FROM a LEFT ANTI JOIN b ON a.id = b.id", "tsql", "LEFT ANTI JOIN"),
        (
            "SELECT * FROM a CROSS APPLY f(a.id)",
            "tsql",
            "JOIN source other than a table",
        ),
        (
            "SELECT * FROM a JOIN LATERAL (SELECT 1) s ON TRUE",
            "postgres",
            "JOIN source other than a table",
        ),
        (
            "SELECT * FROM a JOIN (b JOIN c ON b.id = c.id) ON a.id = b.id",
            None,
            "JOIN source other than a table",
        ),
        ("SELECT * FROM a INNER JOIN b", None, "INNER JOIN without ON or USING"),
        ("SELECT * FROM a LEFT JOIN b", None, "LEFT JOIN without ON or USING"),
        (
            "SELECT * FROM a CROSS JOIN b ON a.id = b.id",
            None,
            "CROSS JOIN with a condition",
        ),
        (
            "SELECT * FROM a c(x, y) JOIN b ON c.x = b.id",
            None,
            "table alias with column names",
        ),
        ("SELECT * FROM a JOIN b WITH (NOLOCK) ON a.id = b.id", "tsql", "table hint"),
        (
            "SELECT * FROM t1, t2 WHERE t1.id = t2.id(+)",
            "oracle",
            "Oracle (+) outer join marker",
        ),
        (
            "SELECT * FROM a JOIN b ON a.id = COUNT(b.id)",
            None,
            "aggregate function COUNT in a JOIN condition",
        ),
    ],
)
def test_unsupported_joins(sql: str, dialect: str | None, message: str) -> None:
    messages = [issue for issue, _ in unsupported_issues(sql, dialect)]

    assert message in messages


def test_oracle_outer_join_marker_explains_the_risk() -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql("SELECT * FROM t1, t2 WHERE t1.id = t2.id(+)", "oracle")

    [issue] = caught.value.issues
    assert issue.hint is not None
    assert "inner join" in issue.hint


def test_unknown_join_shape_is_rejected_not_guessed() -> None:
    # No supported dialect produces "LEFT INNER JOIN"; the translator must fail
    # closed on any side/kind combination it does not recognize.
    tree = parse_sql("SELECT * FROM a LEFT JOIN b ON a.id = b.id")
    tree.args["joins"][0].set("kind", "INNER")

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    assert [issue.message for issue in caught.value.issues] == ["LEFT INNER JOIN"]


def test_qualified_star_outside_the_select_list_is_rejected() -> None:
    # Only valid directly in the SELECT list; anywhere else it is an error even
    # once functions such as COUNT translate their arguments.
    from sqlglot import exp

    from sparkshift.translate import _Translator

    translator = _Translator(None)
    star = exp.Column(this=exp.Star(), table=exp.to_identifier("t"))

    assert translator.expression(star) is None
    assert [issue.message for issue in translator.issues] == ["qualified star"]


# --- Aggregation -------------------------------------------------------------

COUNT_ROWS = ir.AggregateCall(ir.AggregateFunction.COUNT)
AMOUNT = Column(("amount",))
STATUS = Column(("status",))


def sum_of(expression: ir.Expression) -> ir.AggregateCall:
    return ir.AggregateCall(ir.AggregateFunction.SUM, (expression,))


@pytest.mark.parametrize(
    ("sql_call", "expected"),
    [
        ("COUNT(*)", COUNT_ROWS),
        ("COUNT(1)", ir.AggregateCall(ir.AggregateFunction.COUNT, (Literal(1),))),
        ("COUNT(a)", ir.AggregateCall(ir.AggregateFunction.COUNT, (A,))),
        ("COUNT(DISTINCT a)", ir.AggregateCall(ir.AggregateFunction.COUNT, (A,), True)),
        (
            "COUNT(DISTINCT a, b)",
            ir.AggregateCall(ir.AggregateFunction.COUNT, (A, B), True),
        ),
        ("SUM(a)", sum_of(A)),
        ("SUM(DISTINCT a)", ir.AggregateCall(ir.AggregateFunction.SUM, (A,), True)),
        ("AVG(a)", ir.AggregateCall(ir.AggregateFunction.AVG, (A,))),
        ("MIN(a)", ir.AggregateCall(ir.AggregateFunction.MIN, (A,))),
        ("MAX(a)", ir.AggregateCall(ir.AggregateFunction.MAX, (A,))),
    ],
)
def test_aggregate_functions(sql_call: str, expected: ir.AggregateCall) -> None:
    plan = translate_sql(f"SELECT {sql_call} FROM t")

    assert plan == ir.Aggregate(T, (), (expected,))


def test_group_by_with_having_on_a_selected_aggregate() -> None:
    plan = translate_sql(
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status HAVING COUNT(*) > 1"
    )

    assert plan == ir.Filter(
        ir.Aggregate(ORDERS, (STATUS,), (Alias(COUNT_ROWS, "n"),)),
        BinaryOp(BinaryOperator.GREATER, Column(("n",)), Literal(1)),
    )


def test_having_on_an_unselected_aggregate_uses_a_dropped_helper_column() -> None:
    plan = translate_sql(
        "SELECT status FROM orders GROUP BY status HAVING SUM(amount) > 100"
    )

    assert plan == Project(
        ir.Filter(
            ir.Aggregate(ORDERS, (STATUS,), (Alias(sum_of(AMOUNT), "_having_1"),)),
            BinaryOp(BinaryOperator.GREATER, Column(("_having_1",)), Literal(100)),
        ),
        (STATUS,),
    )


def test_having_reuses_a_helper_for_a_repeated_aggregate() -> None:
    plan = translate_sql(
        "SELECT status FROM orders GROUP BY status "
        "HAVING SUM(amount) > 1 AND SUM(amount) < 9"
    )

    assert isinstance(plan, Project)
    assert isinstance(plan.source, ir.Filter)
    assert isinstance(plan.source.source, ir.Aggregate)
    assert plan.source.source.aggregates == (Alias(sum_of(AMOUNT), "_having_1"),)


def test_having_on_a_key_uses_its_output_name() -> None:
    plan = translate_sql(
        "SELECT status AS s, COUNT(*) AS n FROM orders GROUP BY status "
        "HAVING status <> 'x'"
    )

    assert plan == ir.Filter(
        ir.Aggregate(ORDERS, (Alias(STATUS, "s"),), (Alias(COUNT_ROWS, "n"),)),
        BinaryOp(BinaryOperator.NOT_EQUAL, Column(("s",)), Literal("x")),
    )


def test_reordered_select_list_gets_a_final_projection() -> None:
    plan = translate_sql("SELECT COUNT(*) AS n, status FROM orders GROUP BY status")

    assert plan == Project(
        ir.Aggregate(ORDERS, (STATUS,), (Alias(COUNT_ROWS, "n"),)),
        (Column(("n",)), STATUS),
    )


def test_select_in_key_then_aggregate_order_needs_no_projection() -> None:
    plan = translate_sql(
        "SELECT c.country, COUNT(*) FROM customers c GROUP BY c.country"
    )

    assert plan == ir.Aggregate(CUSTOMERS_C, (Column(("c", "country")),), (COUNT_ROWS,))


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT status, COUNT(*) FROM orders GROUP BY 1", None),
        ("SELECT status, COUNT(*) FROM orders GROUP BY 1", "postgres"),
        ("SELECT status AS status, COUNT(*) FROM orders GROUP BY status", None),
        # T-SQL and Oracle always read a GROUP BY name as an input column.
        ("SELECT status AS x, COUNT(*) FROM orders GROUP BY status", "tsql"),
    ],
)
def test_group_by_positions_and_aliases(sql: str, dialect: str | None) -> None:
    plan = translate_sql(sql, dialect)

    assert isinstance(plan, ir.Aggregate)
    assert plan.keys in ((STATUS,), (Alias(STATUS, "x"),))


def test_grouping_without_aggregates() -> None:
    plan = translate_sql("SELECT status FROM orders GROUP BY status, customer_id")

    assert plan == Project(
        ir.Aggregate(ORDERS, (STATUS, Column(("customer_id",))), ()),
        (STATUS,),
    )


def test_aggregates_inside_window_functions_do_not_make_an_aggregation() -> None:
    plan = translate_sql("SELECT a, COUNT(*) OVER () AS n FROM t")

    assert plan == Project(T, (A, Alias(ir.WindowCall(COUNT_ROWS), "n")))


@pytest.mark.parametrize(
    ("sql", "dialect", "message"),
    [
        ("SELECT a, COUNT(*) FROM t GROUP BY 1", "oracle", "GROUP BY position"),
        ("SELECT a, COUNT(*) FROM t GROUP BY 1", "tsql", "GROUP BY position"),
        (
            "SELECT a, COUNT(*) FROM t GROUP BY 3",
            None,
            "GROUP BY position out of range",
        ),
        (
            "SELECT a + 1, COUNT(*) FROM t GROUP BY 1",
            None,
            "GROUP BY position of an expression",
        ),
        (
            "SELECT a AS x, COUNT(*) FROM t GROUP BY x",
            None,
            "GROUP BY name that is also a SELECT alias",
        ),
        ("SELECT COUNT(*) FROM t GROUP BY a + 1", None, "GROUP BY expression"),
        ("SELECT a, COUNT(*) FROM t GROUP BY ROLLUP (a)", None, "ROLLUP"),
        ("SELECT a, COUNT(*) FROM t GROUP BY CUBE (a)", None, "CUBE"),
        (
            "SELECT a, COUNT(*) FROM t GROUP BY GROUPING SETS ((a), ())",
            None,
            "GROUPINGSETS",
        ),
        ("SELECT a, COUNT(*) FROM t GROUP BY a WITH ROLLUP", "mysql", "ROLLUP"),
        ("SELECT a, COUNT(*) FROM t GROUP BY ALL", "snowflake", "GROUP BY ALL"),
        (
            "SELECT a, b, COUNT(*) FROM t GROUP BY a",
            "mysql",
            "column that is neither grouped nor aggregated",
        ),
        (
            "SELECT b, COUNT(*) FROM t",
            None,
            "column that is neither grouped nor aggregated",
        ),
        (
            "SELECT a FROM t GROUP BY a HAVING b > 1",
            None,
            "column that is neither grouped nor aggregated",
        ),
        (
            "SELECT a, COUNT(*) AS n FROM t GROUP BY a HAVING n > 1",
            None,
            "HAVING reference to a SELECT alias",
        ),
        (
            "SELECT COUNT(*), a FROM t GROUP BY a",
            None,
            "aggregate without an alias in a reordered SELECT list",
        ),
        ("SELECT AVG(DISTINCT a) AS x FROM t", None, "AVG(DISTINCT ...)"),
        ("SELECT MIN(DISTINCT a) AS x FROM t", None, "MIN(DISTINCT ...)"),
        (
            "SELECT SUM(MAX(a)) AS x FROM t",
            None,
            "aggregate function MAX in another aggregate function",
        ),
        ("SELECT SUM(DISTINCT a, b) AS x FROM t", None, "SUM with 2 arguments"),
        ("SELECT a FROM t GROUP BY a HAVING my_udf(a)", None, "function MY_UDF"),
    ],
)
def test_unsupported_aggregation(sql: str, dialect: str | None, message: str) -> None:
    messages = [issue for issue, _ in unsupported_issues(sql, dialect)]

    assert message in messages


def test_aggregate_in_where_suggests_having() -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql("SELECT * FROM t WHERE SUM(a) > 1")

    [issue] = caught.value.issues
    assert issue.message == "aggregate function SUM in WHERE"
    assert issue.hint == "Filter on aggregates with HAVING."


def test_oracle_group_by_position_explains_the_difference() -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql("SELECT a, COUNT(*) FROM t GROUP BY 1", "oracle")

    [issue] = caught.value.issues
    assert issue.hint is not None
    assert "constant" in issue.hint


@pytest.mark.parametrize(
    ("sql", "expected_keys"),
    [
        ("SELECT status AS x, COUNT(*) FROM orders GROUP BY 1", (Alias(STATUS, "x"),)),
        # An unqualified key and a qualified column refer to the same key.
        (
            "SELECT o.status, COUNT(*) FROM orders o GROUP BY status",
            (STATUS,),
        ),
    ],
)
def test_group_keys_resolve_positions_and_qualifiers(
    sql: str, expected_keys: tuple[ir.Expression, ...]
) -> None:
    plan = translate_sql(sql)

    assert isinstance(plan, ir.Aggregate)
    assert plan.keys == expected_keys


def test_key_selected_twice_is_also_computed_as_an_expression() -> None:
    plan = translate_sql(
        "SELECT status, status AS again, COUNT(*) AS n FROM orders GROUP BY status"
    )

    assert plan == ir.Aggregate(
        ORDERS, (STATUS,), (Alias(STATUS, "again"), Alias(COUNT_ROWS, "n"))
    )


def test_having_with_not_and_two_different_helpers() -> None:
    plan = translate_sql(
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status "
        "HAVING NOT (SUM(amount) > 1 AND MAX(amount) < 9)"
    )

    max_amount = ir.AggregateCall(ir.AggregateFunction.MAX, (AMOUNT,))
    assert plan == Project(
        ir.Filter(
            ir.Aggregate(
                ORDERS,
                (STATUS,),
                (
                    Alias(COUNT_ROWS, "n"),
                    Alias(sum_of(AMOUNT), "_having_1"),
                    Alias(max_amount, "_having_2"),
                ),
            ),
            UnaryOp(
                UnaryOperator.NOT,
                BinaryOp(
                    BinaryOperator.AND,
                    BinaryOp(
                        BinaryOperator.GREATER, Column(("_having_1",)), Literal(1)
                    ),
                    BinaryOp(BinaryOperator.LESS, Column(("_having_2",)), Literal(9)),
                ),
            ),
        ),
        (STATUS, Column(("n",))),
    )


def test_unknown_parts_of_an_aggregate_call_are_rejected_not_ignored() -> None:
    # No supported syntax produces this today; the allowlist must fail closed.
    tree = parse_sql("SELECT COUNT(a) AS n FROM t")
    tree.expressions[0].this.set("ignore_nulls", exp.true())

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    assert [issue.message for issue in caught.value.issues] == [
        "COUNT with IGNORE_NULLS"
    ]


def test_repeated_group_by_key_is_grouped_once() -> None:
    plan = translate_sql("SELECT status, COUNT(*) FROM orders GROUP BY status, status")

    assert plan == ir.Aggregate(ORDERS, (STATUS,), (COUNT_ROWS,))


# --- Predicates, conditionals, and casts -------------------------------------


@pytest.mark.parametrize(
    ("sql_expression", "expected"),
    [
        ("a IN (1, 2)", ir.InList(A, (Literal(1), Literal(2)))),
        (
            "a NOT IN (1, NULL)",
            UnaryOp(UnaryOperator.NOT, ir.InList(A, (Literal(1), Literal(None)))),
        ),
        ("a BETWEEN 1 AND b", ir.Between(A, Literal(1), B)),
        (
            "a NOT BETWEEN 1 AND 2",
            UnaryOp(UnaryOperator.NOT, ir.Between(A, Literal(1), Literal(2))),
        ),
        ("a LIKE 'x%'", ir.Like(A, "x%")),
        ("a NOT LIKE 'x_'", UnaryOp(UnaryOperator.NOT, ir.Like(A, "x_"))),
        ("a IS NULL", ir.IsNull(A)),
        ("a IS NOT NULL", ir.IsNull(A, negated=True)),
        ("NOT a IS NULL", ir.IsNull(A, negated=True)),
        ("a IS NOT DISTINCT FROM b", ir.NullSafeEqual(A, B)),
        ("a IS DISTINCT FROM b", UnaryOp(UnaryOperator.NOT, ir.NullSafeEqual(A, B))),
        ("COALESCE(a, b, 0)", ir.FunctionCall("coalesce", (A, B, Literal(0)))),
        ("NULLIF(a, 0)", ir.FunctionCall("nullif", (A, Literal(0)))),
        ("CAST(a AS INT)", ir.Cast(A, "int")),
        ("CAST(a AS BIGINT)", ir.Cast(A, "bigint")),
        ("CAST(a AS DECIMAL(10, 2))", ir.Cast(A, "decimal(10,2)")),
        ("CAST(a AS NUMERIC(12))", ir.Cast(A, "decimal(12,0)")),
        ("CAST(a AS DOUBLE PRECISION)", ir.Cast(A, "double")),
        ("CAST(a AS VARCHAR)", ir.Cast(A, "string")),
        ("CAST(a AS TEXT)", ir.Cast(A, "string")),
        ("CAST(a AS DATE)", ir.Cast(A, "date")),
        ("CAST(a AS BOOLEAN)", ir.Cast(A, "boolean")),
        ("TRY_CAST(a AS INT)", ir.Cast(A, "int", safe=True)),
    ],
)
def test_predicates_and_conversions(
    sql_expression: str, expected: ir.Expression
) -> None:
    assert only_item(f"SELECT {sql_expression} AS x FROM t") == Alias(expected, "x")


def test_searched_case() -> None:
    item = only_item(
        "SELECT CASE WHEN a > 1 THEN 'big' WHEN a > 0 THEN 'small' "
        "ELSE 'none' END AS x FROM t"
    )

    assert item == Alias(
        ir.Case(
            (
                (BinaryOp(BinaryOperator.GREATER, A, Literal(1)), Literal("big")),
                (BinaryOp(BinaryOperator.GREATER, A, Literal(0)), Literal("small")),
            ),
            Literal("none"),
        ),
        "x",
    )


def test_simple_case_compares_with_equality() -> None:
    item = only_item("SELECT CASE a WHEN 1 THEN 'one' END AS x FROM t")

    assert item == Alias(
        ir.Case(((BinaryOp(BinaryOperator.EQUAL, A, Literal(1)), Literal("one")),)),
        "x",
    )


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT IF(a > 1, 'x', 'y') AS x FROM t", "mysql"),
        ("SELECT IIF(a > 1, 'x', 'y') AS x FROM t", "tsql"),
    ],
)
def test_if_and_iif_become_case(sql: str, dialect: str) -> None:
    assert only_item(sql, dialect) == Alias(
        ir.Case(
            ((BinaryOp(BinaryOperator.GREATER, A, Literal(1)), Literal("x")),),
            Literal("y"),
        ),
        "x",
    )


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        (
            "SELECT IFNULL(a, 0) AS x FROM t",
            "mysql",
            ir.FunctionCall("coalesce", (A, Literal(0))),
        ),
        (
            "SELECT NVL(a, 0) AS x FROM t",
            "oracle",
            ir.FunctionCall("coalesce", (A, Literal(0))),
        ),
        (
            "SELECT a ILIKE 'x%' AS x FROM t",
            "postgres",
            ir.Like(A, "x%", case_insensitive=True),
        ),
        ("SELECT a <=> b AS x FROM t", "mysql", ir.NullSafeEqual(A, B)),
        (
            "SELECT a::numeric(10, 2) AS x FROM t",
            "postgres",
            ir.Cast(A, "decimal(10,2)"),
        ),
        (
            "SELECT CAST(a AS NUMERIC) AS x FROM t",
            "snowflake",
            ir.Cast(A, "decimal(38,0)"),
        ),
        ("SELECT CAST(a AS SIGNED) AS x FROM t", "mysql", ir.Cast(A, "bigint")),
        ("SELECT CAST(a AS VARCHAR) AS x FROM t", "postgres", ir.Cast(A, "string")),
    ],
)
def test_dialect_spellings(sql: str, dialect: str, expected: ir.Expression) -> None:
    assert only_item(sql, dialect) == Alias(expected, "x")


def test_predicates_work_in_where_and_inside_aggregates() -> None:
    plan = translate_sql(
        "SELECT SUM(CASE WHEN a IN (1, 2) THEN 1 ELSE 0 END) AS n FROM t "
        "WHERE b IS NOT NULL"
    )

    case = ir.Case(((ir.InList(A, (Literal(1), Literal(2))), Literal(1)),), Literal(0))
    assert plan == ir.Aggregate(
        ir.Filter(T, ir.IsNull(B, negated=True)),
        (),
        (Alias(ir.AggregateCall(ir.AggregateFunction.SUM, (case,)), "n"),),
    )


def test_grouping_check_sees_columns_inside_case() -> None:
    # Before the generic child visitor, a column hidden in CASE escaped the check.
    issues = unsupported_issues(
        "SELECT a, CASE WHEN b > 1 THEN 1 END AS x, COUNT(*) AS n FROM t GROUP BY a"
    )

    assert issues == [("column that is neither grouped nor aggregated", "b")]


@pytest.mark.parametrize(
    ("sql", "dialect", "message"),
    [
        ("SELECT a FROM t WHERE a IN (SELECT b FROM u)", None, "IN with a subquery"),
        (
            "SELECT a FROM t WHERE a LIKE b",
            None,
            "LIKE with a pattern that is not a constant",
        ),
        ("SELECT a FROM t WHERE a LIKE 'x\\_y'", None, "LIKE pattern with a backslash"),
        ("SELECT a FROM t WHERE a LIKE '[a-c]%'", "tsql", "LIKE pattern with [ ]"),
        ("SELECT a FROM t WHERE a IS TRUE", None, "IS TRUE"),
        ("SELECT ISNULL(a, 1.5) AS x FROM t", "tsql", "ISNULL"),
        ("SELECT CAST(a AS FLOAT) AS x FROM t", "tsql", "CAST to FLOAT"),
        ("SELECT CAST(a AS REAL) AS x FROM t", None, "CAST to FLOAT"),
        ("SELECT CAST(a AS VARCHAR(10)) AS x FROM t", None, "CAST to VARCHAR(10)"),
        ("SELECT CAST(a AS CHAR(3)) AS x FROM t", None, "CAST to CHAR(3)"),
        ("SELECT CAST(a AS DECIMAL) AS x FROM t", None, "CAST to DECIMAL"),
        ("SELECT CAST(a AS TINYINT) AS x FROM t", "tsql", "CAST to TINYINT"),
        ("SELECT CAST(a AS TIMESTAMP(3)) AS x FROM t", None, "CAST to TIMESTAMP(3)"),
        (
            "SELECT CAST(a AS VARCHAR) AS x FROM t",
            "tsql",
            "CAST to VARCHAR without a length",
        ),
        ("SELECT a BETWEEN 1 AND 2 FROM t", None, "BETWEEN without an alias"),
        ("SELECT NOT a BETWEEN 1 AND 2 FROM t", None, "BETWEEN without an alias"),
        ("SELECT IF(a, 1, 2) FROM t", "mysql", "IF without an alias"),
        ("SELECT CONVERT(INT, a) AS x FROM t", "tsql", "function CONVERT"),
    ],
)
def test_unsupported_predicates_and_casts(
    sql: str, dialect: str | None, message: str
) -> None:
    messages = [issue for issue, _ in unsupported_issues(sql, dialect)]

    assert message in messages


@pytest.mark.parametrize(
    ("sql", "dialect", "hint_fragment"),
    [
        ("SELECT ISNULL(a, 1.5) AS x FROM t", "tsql", "first argument's type"),
        ("SELECT CAST(a AS VARCHAR) AS x FROM t", "tsql", "VARCHAR(30)"),
        ("SELECT CAST(a AS FLOAT) AS x FROM t", "tsql", "8 bytes"),
        ("SELECT a FROM t WHERE a LIKE '[a-c]%'", "tsql", "character class"),
    ],
)
def test_dialect_landmines_explain_themselves(
    sql: str, dialect: str, hint_fragment: str
) -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql(sql, dialect)

    [issue] = caught.value.issues
    assert issue.hint is not None
    assert hint_fragment in issue.hint


def test_case_reports_issues_in_every_part() -> None:
    issues = unsupported_issues(
        "SELECT CASE f(a) WHEN g(b) THEN h(c) ELSE k(d) END AS x FROM t"
    )

    assert [message for message, _ in issues] == [
        "function F",
        "function G",
        "function H",
        "function K",
    ]


def test_unknown_parts_of_predicates_are_rejected_not_ignored() -> None:
    tree = parse_sql("SELECT a FROM t WHERE a BETWEEN 1 AND 2")
    tree.args["where"].this.set("symmetric", exp.true())

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    assert [issue.message for issue in caught.value.issues] == [
        "BETWEEN with SYMMETRIC"
    ]


@pytest.mark.parametrize(
    ("sql_expression", "dialect"),
    [
        ("NOT f(a)", None),
        ("f(a) IS NULL", None),
        ("f(a) IS NOT NULL", None),
        ("f(a) IN (1, 2)", None),
        ("f(a) BETWEEN 1 AND 2", None),
        ("f(a) LIKE 'x%'", None),
        ("f(a) <=> b", "mysql"),
        ("IF(f(a), 1, 2)", "mysql"),
        ("COALESCE(f(a), 1)", None),
        ("NULLIF(f(a), 1)", None),
        ("CAST(f(a) AS INT)", None),
    ],
)
def test_issues_inside_predicates_are_reported(
    sql_expression: str, dialect: str | None
) -> None:
    issues = unsupported_issues(f"SELECT {sql_expression} AS x FROM t", dialect)

    assert issues == [("function F", "F(a)")]


@pytest.mark.parametrize("precision", ["DECIMAL(40, 2)", "DECIMAL(5, 6)"])
def test_out_of_range_decimal_casts_are_rejected(precision: str) -> None:
    messages = [
        m for m, _ in unsupported_issues(f"SELECT CAST(a AS {precision}) AS x FROM t")
    ]

    assert messages == [f"CAST to {precision}"]


@pytest.mark.parametrize(
    ("sql_expression", "dialect", "node_type", "name"),
    [
        ("a LIKE 'x%'", None, exp.Like, "LIKE"),
        ("CASE WHEN a THEN 1 END", None, exp.Case, "CASE"),
        ("IF(a, 1, 2)", "mysql", exp.If, "IF"),
        ("COALESCE(a, 1)", None, exp.Coalesce, "COALESCE"),
        ("NULLIF(a, 1)", None, exp.Nullif, "NULLIF"),
        ("CAST(a AS INT)", None, exp.Cast, "CAST"),
    ],
)
def test_unknown_parts_of_conditionals_are_rejected_not_ignored(
    sql_expression: str, dialect: str | None, node_type: type, name: str
) -> None:
    # Simulates a slot a future SQLGlot version might add: fail closed.
    tree = parse_sql(f"SELECT {sql_expression} AS x FROM t", dialect)
    tree.find(node_type).set("extra", exp.true())

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree, dialect)

    assert [issue.message for issue in caught.value.issues] == [f"{name} with EXTRA"]


# --- String and numeric functions --------------------------------------------


def call(name: str, *arguments: ir.Expression) -> ir.FunctionCall:
    return ir.FunctionCall(name, arguments)


@pytest.mark.parametrize(
    ("sql_expression", "expected"),
    [
        ("UPPER(a)", call("upper", A)),
        ("LOWER(a)", call("lower", A)),
        ("LENGTH(a)", call("length", A)),
        ("CHAR_LENGTH(a)", call("length", A)),
        ("TRIM(a)", call("trim", A)),
        ("LTRIM(a)", call("ltrim", A)),
        ("RTRIM(a)", call("rtrim", A)),
        ("SUBSTRING(a, 2, 3)", call("substring", A, Literal(2), Literal(3))),
        ("SUBSTRING(a FROM 2 FOR 3)", call("substring", A, Literal(2), Literal(3))),
        ("SUBSTR(a, 2)", call("substr", A, Literal(2))),
        ("CONCAT(a, '-', b)", call("concat", A, Literal("-"), B)),
        ("a || b || c", call("concat", A, B, C)),
        ("REPLACE(a, 'x', 'y')", call("replace", A, Literal("x"), Literal("y"))),
        ("LEFT(a, 2)", call("left", A, Literal(2))),
        ("RIGHT(a, 0)", call("right", A, Literal(0))),
        ("ABS(a)", call("abs", A)),
        ("ROUND(a)", call("round", A)),
        ("ROUND(a, 2)", call("round", A, Literal(2))),
        ("ROUND(a, -1)", call("round", A, Literal(-1))),
        ("CEIL(a)", call("ceil", A)),
        ("CEILING(a)", call("ceil", A)),
        ("FLOOR(a)", call("floor", A)),
        ("POWER(a, 2)", call("pow", A, Literal(2))),
        ("SQRT(a)", call("sqrt", A)),
        ("SIGN(a)", call("sign", A)),
        ("LN(a)", call("ln", A)),
        ("EXP(a)", call("exp", A)),
        ("LOG(10, a)", call("log", Literal(10.0), A)),
        ("GREATEST(a, b)", call("greatest", A, B)),
        ("LEAST(a, b, 0)", call("least", A, B, Literal(0))),
    ],
)
def test_functions(sql_expression: str, expected: ir.Expression) -> None:
    assert only_item(f"SELECT {sql_expression} AS x FROM t") == Alias(expected, "x")


@pytest.mark.parametrize(
    ("sql_expression", "dialect", "expected"),
    [
        # T-SQL LEN ignores trailing spaces; SQLGlot's own conversion does not.
        ("LEN(a)", "tsql", call("length", call("rtrim", A))),
        ("LEFT(a, 2)", "tsql", call("left", A, Literal(2))),
        # MySQL LENGTH counts bytes, CHAR_LENGTH characters.
        ("LENGTH(a)", "mysql", call("octet_length", A)),
        ("CHAR_LENGTH(a)", "mysql", call("length", A)),
        # These dialects' CONCAT skip NULL inputs, like concat_ws.
        ("CONCAT(a, b)", "postgres", call("concat_ws", Literal(""), A, B)),
        ("CONCAT(a, b)", "tsql", call("concat_ws", Literal(""), A, B)),
        ("CONCAT(a, b)", "oracle", call("concat_ws", Literal(""), A, B)),
        ("a || b", "oracle", call("concat_ws", Literal(""), A, B)),
        ("a || b", "postgres", call("concat", A, B)),
        ("LOG(a)", "tsql", call("ln", A)),
        ("LOG(a, 2)", "tsql", call("log", Literal(2.0), A)),
        ("SUBSTRING(a, 2)", "snowflake", call("substr", A, Literal(2))),
        ("ROUND(a, 1)", "snowflake", call("round", A, Literal(1))),
        ("GREATEST(a, b)", "postgres", call("greatest", A, B)),
        ("CAST(a AS VARCHAR(MAX))", "tsql", ir.Cast(A, "string")),
    ],
)
def test_dialect_function_semantics(
    sql_expression: str, dialect: str, expected: ir.Expression
) -> None:
    assert only_item(f"SELECT {sql_expression} AS x FROM t", dialect) == Alias(
        expected, "x"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT UPPER(a) FROM t",
        "SELECT CEILING(a) + 1 FROM t",
        "SELECT SUM(ABS(a)) FROM t",
        "SELECT COALESCE(a, 0) FROM t",
    ],
)
def test_function_results_need_an_alias(sql: str) -> None:
    messages = [message for message, _ in unsupported_issues(sql)]

    assert messages == ["function result without an alias"]


def test_mysql_or_operator_is_not_concatenation() -> None:
    # In MySQL, || is a logical OR.
    assert only_item("SELECT a || b AS x FROM t", "mysql") == Alias(
        BinaryOp(BinaryOperator.OR, A, B), "x"
    )


@pytest.mark.parametrize(
    ("sql_expression", "dialect", "message"),
    [
        ("TRIM(BOTH 'x' FROM a)", None, "TRIM with EXPRESSION"),
        ("TRIM(a)", "bigquery", "TRIM"),
        (
            "SUBSTRING(a, -2)",
            None,
            "SUBSTRING start that is not a constant integer of at least 1",
        ),
        (
            "SUBSTRING(a, 0, 2)",
            None,
            "SUBSTRING start that is not a constant integer of at least 1",
        ),
        (
            "SUBSTRING(a, b, 2)",
            None,
            "SUBSTRING start that is not a constant integer of at least 1",
        ),
        (
            "SUBSTRING(a, 1, -1)",
            None,
            "SUBSTRING length that is not a constant integer of at least 0",
        ),
        (
            "LEFT(a, b)",
            None,
            "LEFT length that is not a constant integer of at least 0",
        ),
        (
            "RIGHT(a, -1)",
            None,
            "RIGHT length that is not a constant integer of at least 0",
        ),
        ("ROUND(a)", "postgres", "ROUND"),
        ("ROUND(a)", "mysql", "ROUND"),
        ("ROUND(a)", "oracle", "ROUND"),
        ("ROUND(a, 2, 1)", "tsql", "ROUND with a truncation argument"),
        ("ROUND(a, b)", None, "ROUND scale that is not a constant integer"),
        ("LOG(a)", "postgres", "LOG with one argument"),
        ("LOG(b, a)", None, "LOG with a base that is not a positive constant"),
        ("LOG(0, a)", None, "LOG with a base that is not a positive constant"),
        ("GREATEST(a, b)", "mysql", "GREATEST"),
        ("LEAST(a, b)", "snowflake", "LEAST"),
        ("GREATEST(a, b)", "oracle", "GREATEST"),
    ],
)
def test_unsupported_functions(
    sql_expression: str, dialect: str | None, message: str
) -> None:
    messages = [
        m
        for m, _ in unsupported_issues(f"SELECT {sql_expression} AS x FROM t", dialect)
    ]

    assert message in messages


@pytest.mark.parametrize(
    ("sql_expression", "dialect", "hint_fragment"),
    [
        ("ROUND(a)", "postgres", "halves to even"),
        ("GREATEST(a, b)", "mysql", "returns NULL"),
        ("LOG(a)", "postgres", "base 10"),
        ("TRIM(a)", "bigquery", "tabs and newlines"),
    ],
)
def test_function_landmines_explain_themselves(
    sql_expression: str, dialect: str, hint_fragment: str
) -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql(f"SELECT {sql_expression} AS x FROM t", dialect)

    [issue] = caught.value.issues
    assert issue.hint is not None
    assert hint_fragment in issue.hint


@pytest.mark.parametrize(
    "sql_expression",
    [
        "UPPER(f(a))",
        "LENGTH(f(a))",
        "TRIM(f(a))",
        "SUBSTRING(f(a), 1, 2)",
        "SUBSTR(f(a), 1)",
        "LEFT(f(a), 1)",
        "CONCAT(f(a), b)",
        "REPLACE(f(a), 'x', 'y')",
        "ROUND(f(a))",
        "ROUND(f(a), 1)",
        "POWER(f(a), 2)",
        "LOG(10, f(a))",
        "GREATEST(f(a), 1)",
    ],
)
def test_issues_inside_functions_are_reported(sql_expression: str) -> None:
    issues = unsupported_issues(f"SELECT {sql_expression} AS x FROM t")

    assert issues == [("function F", "F(a)")]


@pytest.mark.parametrize(
    ("sql_expression", "node_type", "name"),
    [
        ("UPPER(a)", exp.Upper, "UPPER"),
        ("LENGTH(a)", exp.Length, "LENGTH"),
        ("SUBSTRING(a, 1, 2)", exp.Substring, "SUBSTRING"),
        ("LEFT(a, 1)", exp.Left, "LEFT"),
        ("CONCAT(a, b)", exp.Concat, "CONCAT"),
        ("REPLACE(a, 'x', 'y')", exp.Replace, "REPLACE"),
        ("ROUND(a)", exp.Round, "ROUND"),
        ("POWER(a, 2)", exp.Pow, "POWER"),
        ("LOG(10, a)", exp.Log, "LOG"),
        ("GREATEST(a, b)", exp.Greatest, "GREATEST"),
    ],
)
def test_unknown_parts_of_functions_are_rejected_not_ignored(
    sql_expression: str, node_type: type, name: str
) -> None:
    tree = parse_sql(f"SELECT {sql_expression} AS x FROM t")
    tree.find(node_type).set("extra", exp.true())

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    assert [issue.message for issue in caught.value.issues] == [f"{name} with EXTRA"]


# --- Dates and timestamps ----------------------------------------------------

D = Column(("d",))


def plus(base: ir.Expression, unit: str, amount: int) -> ir.BinaryOp:
    return BinaryOp(BinaryOperator.ADD, base, ir.Interval(unit, Literal(amount)))


@pytest.mark.parametrize(
    ("sql_expression", "dialect", "expected"),
    [
        ("EXTRACT(YEAR FROM d)", None, call("year", D)),
        ("EXTRACT(QUARTER FROM d)", None, call("quarter", D)),
        ("EXTRACT(DAY FROM d)", None, call("dayofmonth", D)),
        ("EXTRACT(MINUTE FROM d)", None, call("minute", D)),
        ("DATEPART(hour, d)", "tsql", call("hour", D)),
        ("YEAR(d)", None, call("year", D)),
        # T-SQL and MySQL wrap the argument in a CAST to DATE, which is removed.
        ("MONTH(d)", "tsql", call("month", D)),
        ("DAY(d)", "mysql", call("dayofmonth", D)),
        ("CURRENT_DATE", None, call("current_date")),
        ("CURRENT_TIMESTAMP", None, call("current_timestamp")),
        ("GETDATE()", "tsql", call("current_timestamp")),
        ("NOW()", "postgres", call("current_timestamp")),
        # Every dialect's day difference becomes datediff(end, start).
        ("DATEDIFF(a, b)", None, call("datediff", A, B)),
        ("DATEDIFF(day, a, b)", "tsql", call("datediff", B, A)),
        ("DATEDIFF(day, a, b)", "snowflake", call("datediff", B, A)),
        ("DATEDIFF(a, b)", "mysql", call("datediff", A, B)),
        ("DATE_DIFF(a, b, DAY)", "bigquery", call("datediff", A, B)),
        ("d + INTERVAL '3' DAY", None, plus(D, "days", 3)),
        ("INTERVAL '3' DAY + d", None, plus(D, "days", 3)),
        ("d + INTERVAL '3 days'", "postgres", plus(D, "days", 3)),
        (
            "d - INTERVAL '1' MONTH",
            None,
            BinaryOp(BinaryOperator.SUBTRACT, D, ir.Interval("months", Literal(1))),
        ),
        ("d + INTERVAL '2' WEEK", None, plus(D, "weeks", 2)),
        ("d + INTERVAL '1' YEAR", None, plus(D, "years", 1)),
        ("DATEADD(day, 3, d)", "tsql", plus(D, "days", 3)),
        (
            "DATEADD(month, a, d)",
            "snowflake",
            BinaryOp(BinaryOperator.ADD, D, ir.Interval("months", A)),
        ),
        ("DATE_ADD(d, INTERVAL 3 DAY)", "mysql", plus(D, "days", 3)),
        (
            "DATE_SUB(d, INTERVAL 3 DAY)",
            "mysql",
            BinaryOp(BinaryOperator.SUBTRACT, D, ir.Interval("days", Literal(3))),
        ),
        ("DATE_ADD(d, 3)", None, call("date_add", D, Literal(3))),
        ("DATE_ADD(d, INTERVAL 3 DAY)", None, plus(D, "days", 3)),
        ("DATE_SUB(d, 3)", None, call("date_sub", D, Literal(3))),
        ("ADD_MONTHS(d, 1)", None, call("add_months", D, Literal(1))),
        ("DATE_TRUNC('MONTH', d)", None, call("date_trunc", Literal("month"), D)),
        (
            "DATE_TRUNC('quarter', d)",
            "postgres",
            call("date_trunc", Literal("quarter"), D),
        ),
        ("DATE_TRUNC(d, YEAR)", "bigquery", call("trunc", D, Literal("year"))),
    ],
)
def test_dates(
    sql_expression: str, dialect: str | None, expected: ir.Expression
) -> None:
    assert only_item(f"SELECT {sql_expression} AS x FROM t", dialect) == Alias(
        expected, "x"
    )


@pytest.mark.parametrize(
    ("type_name", "dialect", "spark_type"),
    [
        ("TIMESTAMP", None, "timestamp"),
        ("TIMESTAMP_NTZ", None, "timestamp_ntz"),
        ("TIMESTAMP", "postgres", "timestamp_ntz"),
        ("TIMESTAMPTZ", "postgres", "timestamp"),
        ("TIMESTAMP", "snowflake", "timestamp_ntz"),
        ("TIMESTAMP_LTZ", "snowflake", "timestamp"),
        ("TIMESTAMP", "oracle", "timestamp_ntz"),
        ("TIMESTAMP WITH TIME ZONE", "oracle", "timestamp"),
        ("DATETIME", "mysql", "timestamp_ntz"),
        ("TIMESTAMP", "mysql", "timestamp"),
        ("DATETIME", "bigquery", "timestamp_ntz"),
        ("TIMESTAMP", "bigquery", "timestamp"),
        ("DATETIME2", "tsql", "timestamp_ntz"),
        ("DATETIMEOFFSET", "tsql", "timestamp"),
    ],
)
def test_timestamp_casts_choose_point_in_time_or_wall_clock(
    type_name: str, dialect: str | None, spark_type: str
) -> None:
    item = only_item(f"SELECT CAST(a AS {type_name}) AS x FROM t", dialect)

    assert item == Alias(ir.Cast(A, spark_type), "x")


@pytest.mark.parametrize(
    ("sql_expression", "dialect", "message"),
    [
        ("EXTRACT(SECOND FROM d)", None, "EXTRACT SECOND"),
        ("EXTRACT(DOW FROM d)", None, "EXTRACT DOW"),
        ("EXTRACT(WEEK FROM d)", None, "EXTRACT WEEK"),
        ("SYSDATE", "oracle", "SYSDATE"),
        ("DATEDIFF(month, a, b)", "tsql", "DATEDIFF in MONTH"),
        ("DATE_DIFF(a, b, WEEK)", "bigquery", "DATEDIFF in WEEK"),
        ("DATEDIFF(day, a, b)", None, "DATEDIFF in an unrecognized form"),
        ("DATEDIFF(a, b)", "postgres", "DATEDIFF"),
        ("DATEADD(hour, 1, d)", "tsql", "date arithmetic in HOUR"),
        ("d + INTERVAL '1' HOUR", None, "date arithmetic in HOUR"),
        (
            "DATE_ADD(d, INTERVAL 'x' DAY)",
            "mysql",
            "interval amount that is not an integer",
        ),
        ("INTERVAL '1' DAY - d", None, "an interval minus a value"),
        ("INTERVAL '1' DAY + INTERVAL '2' DAY", None, "arithmetic on two intervals"),
        ("DATE_ADD(d, 3)", "snowflake", "DATE_ADD without a unit"),
        ("ADD_MONTHS(d, 1)", "oracle", "ADD_MONTHS"),
        ("ADD_MONTHS(d, 1)", "snowflake", "ADD_MONTHS"),
        ("DATE_TRUNC('month', d)", "snowflake", "DATE_TRUNC"),
        ("DATETRUNC(month, d)", "tsql", "DATE_TRUNC"),
        ("TRUNC(d, 'MM')", "oracle", "DATE_TRUNC"),
        ("DATE_TRUNC('WEEK', d)", None, "DATE_TRUNC to WEEK"),
        ("CAST(a AS DATETIME)", "tsql", "CAST to DATETIME"),
        ("CAST(a AS SMALLDATETIME)", "tsql", "CAST to SMALLDATETIME"),
        # SQLGlot renders T-SQL TIMESTAMP as ROWVERSION; the hint explains.
        ("CAST(a AS TIMESTAMP)", "tsql", "CAST to ROWVERSION"),
        ("CAST(a AS DATETIME)", None, "CAST to DATETIME"),
    ],
)
def test_unsupported_dates(
    sql_expression: str, dialect: str | None, message: str
) -> None:
    messages = [
        m
        for m, _ in unsupported_issues(f"SELECT {sql_expression} AS x FROM t", dialect)
    ]

    assert message in messages


@pytest.mark.parametrize(
    ("sql_expression", "dialect", "hint_fragment"),
    [
        ("EXTRACT(DOW FROM d)", None, "Sunday"),
        ("CAST(a AS DATETIME)", "tsql", "1/300"),
        ("CAST(a AS TIMESTAMP)", "tsql", "ROWVERSION"),
        ("ADD_MONTHS(d, 1)", "oracle", "Mar 31"),
        ("DATEDIFF(day, a, b)", None, "two arguments"),
    ],
)
def test_date_landmines_explain_themselves(
    sql_expression: str, dialect: str | None, hint_fragment: str
) -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql(f"SELECT {sql_expression} AS x FROM t", dialect)

    [issue] = caught.value.issues
    assert issue.hint is not None
    assert hint_fragment in issue.hint


def test_unaliased_date_arithmetic_needs_an_alias() -> None:
    messages = [m for m, _ in unsupported_issues("SELECT d + INTERVAL '1' DAY FROM t")]

    assert messages == ["function result without an alias"]


@pytest.mark.parametrize(
    "sql_expression",
    [
        "EXTRACT(YEAR FROM f(a))",
        "YEAR(f(a))",
        "DATEDIFF(f(a), b)",
        "f(a) + INTERVAL '1' DAY",
        "DATE_ADD(f(a), 1)",
        "DATEADD(day, f(a), d)",
        "ADD_MONTHS(f(a), 1)",
        "DATE_TRUNC('MONTH', f(a))",
    ],
)
def test_issues_inside_date_functions_are_reported(sql_expression: str) -> None:
    dialect = "snowflake" if sql_expression.startswith("DATEADD") else None
    issues = unsupported_issues(f"SELECT {sql_expression} AS x FROM t", dialect)

    assert issues == [("function F", "F(a)")]


@pytest.mark.parametrize(
    ("sql_expression", "node_type", "name"),
    [
        ("EXTRACT(YEAR FROM d)", exp.Extract, "EXTRACT"),
        ("YEAR(d)", exp.Year, "YEAR"),
        ("CURRENT_DATE", exp.CurrentDate, "CURRENT_DATE"),
        ("DATEDIFF(a, b)", exp.DateDiff, "DATEDIFF"),
        ("DATE_ADD(d, 1)", exp.DateAdd, "DATE_ADD"),
        ("ADD_MONTHS(d, 1)", exp.AddMonths, "ADD_MONTHS"),
        ("DATE_TRUNC('MONTH', d)", exp.DateTrunc, "DATE_TRUNC"),
    ],
)
def test_unknown_parts_of_date_functions_are_rejected_not_ignored(
    sql_expression: str, node_type: type, name: str
) -> None:
    tree = parse_sql(f"SELECT {sql_expression} AS x FROM t")
    tree.find(node_type).set("extra", exp.true())

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    assert [issue.message for issue in caught.value.issues] == [f"{name} with EXTRA"]


# --- ORDER BY ----------------------------------------------------------------


def ascending(expression: ir.Expression, nulls_first: bool = True) -> ir.SortKey:
    return ir.SortKey(expression, descending=False, nulls_first=nulls_first)


def descending(expression: ir.Expression, nulls_first: bool = False) -> ir.SortKey:
    return ir.SortKey(expression, descending=True, nulls_first=nulls_first)


@pytest.mark.parametrize(
    ("dialect", "nulls_first_ascending"),
    [
        (None, True),
        ("tsql", True),
        ("mysql", True),
        ("bigquery", True),
        ("postgres", False),
        ("oracle", False),
        ("snowflake", False),
    ],
)
def test_null_placement_follows_the_source_dialect(
    dialect: str | None, nulls_first_ascending: bool
) -> None:
    # NULLs sort as the smallest value in some databases and the largest in
    # others, so the same ORDER BY puts them at opposite ends.
    plan = translate_sql("SELECT a, b FROM t ORDER BY a, b DESC", dialect)

    assert plan == ir.Sort(
        Project(T, (A, B)),
        (
            ascending(A, nulls_first=nulls_first_ascending),
            descending(B, nulls_first=not nulls_first_ascending),
        ),
    )


def test_explicit_null_placement_overrides_the_default() -> None:
    plan = translate_sql(
        "SELECT a, b FROM t ORDER BY a NULLS FIRST, b DESC NULLS LAST", "postgres"
    )

    assert plan == ir.Sort(
        Project(T, (A, B)),
        (ascending(A, nulls_first=True), descending(B, nulls_first=False)),
    )


def test_order_by_position_refers_to_the_output_column() -> None:
    plan = translate_sql("SELECT a, b AS x FROM t ORDER BY 2 DESC")

    assert plan == ir.Sort(
        Project(T, (A, Alias(B, "x"))), (descending(Column(("x",))),)
    )


def test_order_by_name_means_the_alias_even_if_it_shadows_a_column() -> None:
    # Standard SQL: a plain ORDER BY name refers to an output column first.
    plan = translate_sql("SELECT b AS a FROM t ORDER BY a")

    assert plan == ir.Sort(Project(T, (Alias(B, "a"),)), (ascending(A),))


def test_order_by_a_selected_expression_uses_its_output_column() -> None:
    double = BinaryOp(BinaryOperator.MULTIPLY, A, Literal(2))
    plan = translate_sql("SELECT a * 2 AS d FROM t ORDER BY a * 2")

    assert plan == ir.Sort(
        Project(T, (Alias(double, "d"),)), (ascending(Column(("d",))),)
    )


def test_order_by_an_unselected_column_sorts_before_the_projection() -> None:
    condition = BinaryOp(BinaryOperator.GREATER, C, Literal(1))
    plan = translate_sql("SELECT a FROM t WHERE c > 1 ORDER BY b")

    assert plan == Project(ir.Sort(ir.Filter(T, condition), (ascending(B),)), (A,))


def test_aliases_become_their_expressions_when_sorting_before_projecting() -> None:
    plus_one = BinaryOp(BinaryOperator.ADD, A, Literal(1))
    plan = translate_sql("SELECT a + 1 AS x FROM t ORDER BY x DESC, b")

    assert plan == Project(
        ir.Sort(T, (descending(plus_one), ascending(B))), (Alias(plus_one, "x"),)
    )


def test_alias_of_the_same_column_may_be_used_in_an_expression() -> None:
    plus_one = BinaryOp(BinaryOperator.ADD, A, Literal(1))
    plan = translate_sql("SELECT a AS a FROM t ORDER BY a + 1")

    assert plan == Project(ir.Sort(T, (ascending(plus_one),)), (Alias(A, "a"),))


def test_select_star_sorts_its_input_directly() -> None:
    assert translate_sql("SELECT * FROM t ORDER BY a") == ir.Sort(T, (ascending(A),))


def test_order_by_comes_after_distinct_and_before_limit() -> None:
    plan = translate_sql("SELECT DISTINCT a FROM t WHERE b > 1 ORDER BY a LIMIT 3")

    assert plan == ir.Limit(
        ir.Sort(
            ir.Distinct(
                Project(
                    ir.Filter(T, BinaryOp(BinaryOperator.GREATER, B, Literal(1))),
                    (A,),
                )
            ),
            (ascending(A),),
        ),
        3,
    )


def test_top_with_order_by_is_a_top_n_query() -> None:
    plan = translate_sql("SELECT TOP 3 a FROM t ORDER BY a DESC", "tsql")

    assert plan == ir.Limit(ir.Sort(Project(T, (A,)), (descending(A),)), 3)


def test_order_by_an_aggregate_alias_sorts_the_aggregated_result() -> None:
    plan = translate_sql(
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC"
    )

    assert plan == ir.Sort(
        ir.Aggregate(ORDERS, (STATUS,), (Alias(COUNT_ROWS, "n"),)),
        (descending(Column(("n",))),),
    )


def test_order_by_an_unselected_aggregate_uses_a_helper_column() -> None:
    total = ir.AggregateCall(ir.AggregateFunction.SUM, (AMOUNT,))
    plan = translate_sql(
        "SELECT status FROM orders GROUP BY status ORDER BY SUM(amount) DESC"
    )

    assert plan == Project(
        ir.Sort(
            ir.Aggregate(ORDERS, (STATUS,), (Alias(total, "_order_1"),)),
            (descending(Column(("_order_1",))),),
        ),
        (STATUS,),
    )


def test_order_by_reuses_a_having_helper_column() -> None:
    plan = translate_sql(
        "SELECT status FROM orders GROUP BY status "
        "HAVING COUNT(*) > 1 ORDER BY COUNT(*) DESC"
    )

    helper = Column(("_having_1",))
    assert plan == Project(
        ir.Sort(
            ir.Filter(
                ir.Aggregate(ORDERS, (STATUS,), (Alias(COUNT_ROWS, "_having_1"),)),
                BinaryOp(BinaryOperator.GREATER, helper, Literal(1)),
            ),
            (descending(helper),),
        ),
        (STATUS,),
    )


def test_order_by_an_unselected_group_key() -> None:
    plan = translate_sql(
        "SELECT COUNT(*) AS n FROM orders GROUP BY status ORDER BY status"
    )

    assert plan == Project(
        ir.Sort(
            ir.Aggregate(ORDERS, (STATUS,), (Alias(COUNT_ROWS, "n"),)),
            (ascending(STATUS),),
        ),
        (Column(("n",)),),
    )


@pytest.mark.parametrize(
    ("sql", "dialect", "message"),
    [
        (
            "SELECT DISTINCT a FROM t ORDER BY b",
            None,
            "ORDER BY key that is not a column of the SELECT DISTINCT list",
        ),
        (
            "SELECT a AS x FROM t ORDER BY x + 1",
            None,
            "SELECT alias inside an ORDER BY expression",
        ),
        (
            "SELECT a AS x, b AS x FROM t ORDER BY x",
            None,
            "ORDER BY name that matches several SELECT items",
        ),
        ("SELECT a FROM t ORDER BY NULL", "mysql", "ORDER BY a constant"),
        ("SELECT a FROM t ORDER BY 'a'", None, "ORDER BY a constant"),
        ("SELECT a FROM t ORDER BY -1", None, "ORDER BY a constant"),
        ("SELECT a FROM t ORDER BY 2", None, "ORDER BY position out of range"),
        ("SELECT a FROM t ORDER BY 0", None, "ORDER BY position out of range"),
        ("SELECT * FROM t ORDER BY 1", None, "ORDER BY position of *"),
        (
            "SELECT status, COUNT(*) FROM orders GROUP BY status ORDER BY 2",
            None,
            "ORDER BY position of a SELECT item without a unique name",
        ),
        (
            "SELECT a FROM t ORDER BY COUNT(*)",
            None,
            "aggregate function COUNT in ORDER BY without GROUP BY",
        ),
        (
            "SELECT status FROM orders GROUP BY status ORDER BY amount",
            None,
            "column that is neither grouped nor aggregated",
        ),
        ("SELECT a FROM t ORDER SIBLINGS BY a", "oracle", "ORDER BY SIBLINGS"),
    ],
)
def test_unsupported_order_by(sql: str, dialect: str | None, message: str) -> None:
    assert message in [issue for issue, _ in unsupported_issues(sql, dialect)]


def test_every_order_by_issue_is_reported() -> None:
    issues = unsupported_issues("SELECT a AS x FROM t ORDER BY x + 1, NULL, 5")

    assert issues == [
        ("SELECT alias inside an ORDER BY expression", "x"),
        ("ORDER BY a constant", "NULL"),
        ("ORDER BY position out of range", "5"),
    ]


def test_distinct_reports_only_the_keys_it_does_not_output() -> None:
    issues = unsupported_issues("SELECT DISTINCT a FROM t ORDER BY a, b")

    assert issues == [
        ("ORDER BY key that is not a column of the SELECT DISTINCT list", "b")
    ]


def test_unknown_parts_of_a_sort_key_are_rejected() -> None:
    # A part SQLGlot may parse in other dialects, such as ClickHouse WITH FILL,
    # must never be silently dropped.
    tree = parse_sql("SELECT a FROM t ORDER BY a")
    tree.args["order"].expressions[0].set("with_fill", exp.WithFill())

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    [issue] = caught.value.issues
    assert issue.message == "ORDER BY WITH_FILL"


# --- Window functions --------------------------------------------------------


def window_item(sql: str, dialect: str | None = None) -> ir.WindowCall:
    [item] = select_items(sql, dialect)
    assert isinstance(item, Alias)
    assert isinstance(item.expression, ir.WindowCall)
    return item.expression


@pytest.mark.parametrize(
    ("function", "ranking"),
    [
        ("ROW_NUMBER", ir.WindowFunction.ROW_NUMBER),
        ("RANK", ir.WindowFunction.RANK),
        ("DENSE_RANK", ir.WindowFunction.DENSE_RANK),
    ],
)
def test_ranking_functions(function: str, ranking: ir.WindowFunction) -> None:
    call = window_item(
        f"SELECT {function}() OVER (PARTITION BY a ORDER BY b DESC) AS r FROM t"
    )

    assert call == ir.WindowCall(ranking, (A,), (descending(B),))


def test_aggregate_over_a_partition() -> None:
    call = window_item("SELECT SUM(c) OVER (PARTITION BY a, b) AS s FROM t")

    assert call == ir.WindowCall(
        ir.AggregateCall(ir.AggregateFunction.SUM, (C,)), (A, B)
    )


def test_count_rows_over_the_whole_result() -> None:
    assert window_item("SELECT COUNT(*) OVER () AS n FROM t") == ir.WindowCall(
        COUNT_ROWS
    )


def test_running_aggregate_keeps_its_window_order() -> None:
    call = window_item("SELECT AVG(c) OVER (ORDER BY a, b DESC) AS s FROM t")

    assert call == ir.WindowCall(
        ir.AggregateCall(ir.AggregateFunction.AVG, (C,)),
        (),
        (ascending(A), descending(B)),
    )


@pytest.mark.parametrize(
    ("sql", "dialect", "key"),
    [
        ("SELECT RANK() OVER (ORDER BY b) AS r FROM t", None, ascending(B)),
        (
            "SELECT RANK() OVER (ORDER BY b) AS r FROM t",
            "postgres",
            ascending(B, nulls_first=False),
        ),
        (
            "SELECT RANK() OVER (ORDER BY b DESC) AS r FROM t",
            "oracle",
            descending(B, nulls_first=True),
        ),
        (
            "SELECT RANK() OVER (ORDER BY b NULLS LAST) AS r FROM t",
            None,
            ascending(B, nulls_first=False),
        ),
    ],
)
def test_window_null_placement_follows_the_source_dialect(
    sql: str, dialect: str | None, key: ir.SortKey
) -> None:
    assert window_item(sql, dialect).order_by == (key,)


def test_windows_are_computed_after_where_and_before_order_by() -> None:
    plan = translate_sql(
        "SELECT a, ROW_NUMBER() OVER (ORDER BY a) AS rn FROM t WHERE b > 1 ORDER BY rn"
    )

    numbered = ir.WindowCall(ir.WindowFunction.ROW_NUMBER, (), (ascending(A),))
    assert plan == ir.Sort(
        Project(
            ir.Filter(T, BinaryOp(BinaryOperator.GREATER, B, Literal(1))),
            (A, Alias(numbered, "rn")),
        ),
        (ascending(Column(("rn",))),),
    )


def test_window_next_to_select_star() -> None:
    plan = translate_sql("SELECT *, RANK() OVER (ORDER BY a) AS r FROM t")

    ranked = ir.WindowCall(ir.WindowFunction.RANK, (), (ascending(A),))
    assert plan == Project(T, (Star(), Alias(ranked, "r")))


@pytest.mark.parametrize(
    ("sql", "dialect", "message"),
    [
        (
            "SELECT a FROM t WHERE ROW_NUMBER() OVER (ORDER BY a) = 1",
            None,
            "window function outside the SELECT list",
        ),
        (
            "SELECT a FROM t ORDER BY RANK() OVER (ORDER BY a)",
            None,
            "window function outside the SELECT list",
        ),
        (
            "SELECT a, RANK() OVER (ORDER BY SUM(b)) AS r FROM t GROUP BY a",
            None,
            "window function in an aggregate query",
        ),
        (
            "SELECT SUM(SUM(b)) OVER () AS s FROM t",
            None,
            "aggregate function SUM in another aggregate function",
        ),
        (
            "SELECT RANK() OVER (PARTITION BY ROW_NUMBER() OVER (ORDER BY a) "
            "ORDER BY b) AS r FROM t",
            None,
            "window function inside a window",
        ),
        (
            "SELECT ROW_NUMBER() OVER (PARTITION BY SUM(a) ORDER BY b) AS r FROM t",
            None,
            "aggregate function SUM in a window's PARTITION BY or ORDER BY",
        ),
        (
            "SELECT SUM(a) OVER (ORDER BY b GROUPS BETWEEN 1 PRECEDING AND "
            "CURRENT ROW) AS s FROM t",
            "postgres",
            "GROUPS window frame",
        ),
        (
            "SELECT SUM(a) OVER w AS s FROM t WINDOW w AS (PARTITION BY b)",
            None,
            "named window",
        ),
        (
            "SELECT NTH_VALUE(a, 2) OVER (ORDER BY b) AS v FROM t",
            None,
            "window function NTH_VALUE",
        ),
        (
            "SELECT LEAD(a) IGNORE NULLS OVER (ORDER BY b) AS f FROM t",
            None,
            "IGNORE NULLS on LEAD",
        ),
        (
            "SELECT COUNT(DISTINCT a) OVER (PARTITION BY b) AS n FROM t",
            None,
            "COUNT(DISTINCT ...) over a window",
        ),
        ("SELECT RANK(a) OVER (ORDER BY b) AS r FROM t", None, "RANK with arguments"),
        (
            "SELECT MAX(a) KEEP (DENSE_RANK FIRST ORDER BY b) OVER (PARTITION BY c) "
            "AS m FROM t",
            "oracle",
            "KEEP (DENSE_RANK FIRST/LAST ...)",
        ),
        (
            "SELECT ROW_NUMBER() OVER (ORDER BY 1) AS rn FROM t",
            None,
            "constant in a window ORDER BY",
        ),
        (
            "SELECT ROW_NUMBER() OVER (ORDER BY a) FROM t",
            None,
            "window function without an alias",
        ),
        (
            "SELECT a, ROW_NUMBER() OVER (ORDER BY a) AS rn FROM t ORDER BY b",
            None,
            "ORDER BY key that is not selected, in a query with window functions",
        ),
    ],
)
def test_unsupported_window(sql: str, dialect: str | None, message: str) -> None:
    assert message in [issue for issue, _ in unsupported_issues(sql, dialect)]


def test_every_window_issue_is_reported() -> None:
    issues = unsupported_issues("SELECT NTH_VALUE(a, 2) OVER (ORDER BY 1) AS v FROM t")

    assert issues == [
        ("window function NTH_VALUE", "NTH_VALUE(a, 2)"),
        ("constant in a window ORDER BY", "1"),
    ]


def test_order_by_a_window_alias_is_allowed_with_distinct() -> None:
    plan = translate_sql(
        "SELECT DISTINCT a, DENSE_RANK() OVER (ORDER BY a) AS r FROM t ORDER BY r"
    )

    ranked = ir.WindowCall(ir.WindowFunction.DENSE_RANK, (), (ascending(A),))
    assert plan == ir.Sort(
        ir.Distinct(Project(T, (A, Alias(ranked, "r")))),
        (ascending(Column(("r",))),),
    )


def test_windows_report_only_the_sort_keys_they_do_not_output() -> None:
    issues = unsupported_issues(
        "SELECT a, ROW_NUMBER() OVER (ORDER BY a) AS rn FROM t ORDER BY rn, b"
    )

    assert issues == [
        ("ORDER BY key that is not selected, in a query with window functions", "b")
    ]


def test_window_sort_key_issues_are_reported() -> None:
    issues = unsupported_issues("SELECT RANK() OVER (ORDER BY my_udf(a)) AS r FROM t")

    assert issues == [("function MY_UDF", "MY_UDF(a)")]


def test_unknown_parts_of_a_window_sort_key_are_rejected() -> None:
    tree = parse_sql("SELECT RANK() OVER (ORDER BY a) AS r FROM t")
    tree.find(exp.Window).args["order"].expressions[0].set("with_fill", exp.WithFill())

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    [issue] = caught.value.issues
    assert issue.message == "window ORDER BY with WITH_FILL"


def test_unsupported_expression_is_named_by_its_kind() -> None:
    issues = unsupported_issues("SELECT (SELECT 1) AS x FROM t")

    assert issues == [("SUBQUERY expression", "(SELECT 1)")]


# --- Offset functions and window frames --------------------------------------

ORDER_BY_B = (ascending(B),)


@pytest.mark.parametrize(
    ("call", "function"),
    [
        ("LAG(a)", ir.FunctionCall("lag", (A,))),
        ("LEAD(a, 2)", ir.FunctionCall("lead", (A, Literal(2)))),
        ("LAG(a, 1, 0)", ir.FunctionCall("lag", (A, Literal(1), Literal(0)))),
        # A NULL default is the default: nothing to pass.
        ("LAG(a, 3, NULL)", ir.FunctionCall("lag", (A, Literal(3)))),
        (
            "LEAD(a, 1, -2.5)",
            ir.FunctionCall("lead", (A, Literal(1), Literal(Decimal("-2.5")))),
        ),
        ("LAG(a, 1, 'none')", ir.FunctionCall("lag", (A, Literal(1), Literal("none")))),
        # RESPECT NULLS is every function's default.
        ("LAG(a) RESPECT NULLS", ir.FunctionCall("lag", (A,))),
        ("FIRST_VALUE(a)", ir.FunctionCall("first_value", (A,))),
        ("LAST_VALUE(a)", ir.FunctionCall("last_value", (A,))),
        (
            "LAST_VALUE(a) IGNORE NULLS",
            ir.FunctionCall("last_value", (A, Literal(True))),
        ),
        ("NTILE(4)", ir.FunctionCall("ntile", (Literal(4),))),
    ],
)
def test_offset_and_value_functions(call: str, function: ir.FunctionCall) -> None:
    window = window_item(f"SELECT {call} OVER (ORDER BY b) AS v FROM t")

    assert window == ir.WindowCall(function, (), ORDER_BY_B)


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ("ROWS BETWEEN 2 PRECEDING AND CURRENT ROW", ir.WindowFrame(True, -2, 0)),
        # A frame with only a start ends at the current row.
        ("ROWS 3 PRECEDING", ir.WindowFrame(True, -3, 0)),
        ("ROWS UNBOUNDED PRECEDING", ir.WindowFrame(True, None, 0)),
        ("ROWS BETWEEN CURRENT ROW AND 2 FOLLOWING", ir.WindowFrame(True, 0, 2)),
        ("ROWS BETWEEN 1 FOLLOWING AND 3 FOLLOWING", ir.WindowFrame(True, 1, 3)),
        (
            "ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING",
            ir.WindowFrame(True, None, None),
        ),
        (
            "RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
            ir.WindowFrame(False, None, 0),
        ),
        (
            "RANGE BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING",
            ir.WindowFrame(False, 0, None),
        ),
    ],
)
def test_window_frames(frame: str, expected: ir.WindowFrame) -> None:
    window = window_item(f"SELECT SUM(a) OVER (ORDER BY b {frame}) AS s FROM t")

    assert window.frame == expected


def test_value_functions_take_explicit_frames() -> None:
    window = window_item(
        "SELECT LAST_VALUE(a) OVER (ORDER BY b ROWS BETWEEN UNBOUNDED PRECEDING "
        "AND UNBOUNDED FOLLOWING) AS v FROM t",
        "bigquery",
    )

    assert window.frame == ir.WindowFrame(True, None, None)


def test_snowflake_value_functions_default_to_the_whole_window() -> None:
    # Snowflake documents ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED
    # FOLLOWING as the default frame of FIRST_VALUE and LAST_VALUE.
    # SQLGlot's Snowflake parser adds that frame; this pins it.
    window = window_item(
        "SELECT LAST_VALUE(a) OVER (ORDER BY b) AS v FROM t", "snowflake"
    )

    assert window.frame == ir.WindowFrame(True, None, None)


@pytest.mark.parametrize(
    "sql",
    [
        # Without ORDER BY, every frame default covers the whole window.
        "SELECT LAST_VALUE(a) OVER (PARTITION BY c) AS v FROM t",
        # Snowflake's aggregates use SQL's default frame.
        "SELECT SUM(a) OVER (ORDER BY b) AS v FROM t",
    ],
)
def test_snowflake_keeps_the_default_frame_elsewhere(sql: str) -> None:
    assert window_item(sql, "snowflake").frame is None


@pytest.mark.parametrize(
    ("sql", "dialect", "message"),
    [
        (
            "SELECT ROW_NUMBER() OVER () AS rn FROM t",
            None,
            "ROW_NUMBER without ORDER BY",
        ),
        (
            "SELECT LAG(a) OVER (PARTITION BY b) AS l FROM t",
            None,
            "LAG without ORDER BY",
        ),
        (
            "SELECT RANK() OVER (ORDER BY b ROWS 1 PRECEDING) AS r FROM t",
            None,
            "window frame on RANK",
        ),
        (
            "SELECT NTILE(2) OVER (ORDER BY b ROWS 1 PRECEDING) AS n FROM t",
            None,
            "window frame on NTILE",
        ),
        (
            "SELECT LAG(a, -1) OVER (ORDER BY b) AS l FROM t",
            None,
            "LAG offset that is not a non-negative integer",
        ),
        (
            "SELECT LEAD(a, c) OVER (ORDER BY b) AS l FROM t",
            None,
            "LEAD offset that is not a non-negative integer",
        ),
        (
            "SELECT LAG(a, 1, c) OVER (ORDER BY b) AS l FROM t",
            None,
            "LAG default that is not a constant",
        ),
        (
            "SELECT LAG(a, 1, -c) OVER (ORDER BY b) AS l FROM t",
            None,
            "LAG default that is not a constant",
        ),
        (
            "SELECT LAG(a) IGNORE NULLS OVER (ORDER BY b) AS l FROM t",
            "snowflake",
            "IGNORE NULLS on LAG",
        ),
        (
            "SELECT NTILE(0) OVER (ORDER BY b) AS n FROM t",
            None,
            "NTILE with a bucket count that is not a positive integer",
        ),
        (
            "SELECT NTILE(c) OVER (ORDER BY b) AS n FROM t",
            None,
            "NTILE with a bucket count that is not a positive integer",
        ),
        (
            "SELECT FIRST_VALUE(a) OVER (ORDER BY b) AS v FROM t",
            "bigquery",
            "FIRST_VALUE with ORDER BY but no frame",
        ),
        (
            "SELECT SUM(a) OVER (ORDER BY b RANGE BETWEEN 5 PRECEDING AND CURRENT ROW) "
            "AS s FROM t",
            None,
            "RANGE frame with an offset",
        ),
        (
            "SELECT SUM(a) OVER (ORDER BY b ROWS BETWEEN 1 FOLLOWING AND 1 PRECEDING) "
            "AS s FROM t",
            None,
            "window frame that ends before it starts",
        ),
        (
            "SELECT SUM(a) OVER (PARTITION BY b ROWS 1 PRECEDING) AS s FROM t",
            None,
            "window frame without ORDER BY",
        ),
        (
            "SELECT SUM(a) OVER (ORDER BY b ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING "
            "EXCLUDE CURRENT ROW) AS s FROM t",
            "postgres",
            "window frame with EXCLUDE",
        ),
        (
            "SELECT SUM(a) OVER (ORDER BY b ROWS BETWEEN c PRECEDING AND CURRENT ROW) "
            "AS s FROM t",
            None,
            "window frame offset that is not a non-negative integer",
        ),
        (
            "SELECT SUM(a) OVER (ORDER BY b ROWS BETWEEN UNBOUNDED FOLLOWING AND "
            "CURRENT ROW) AS s FROM t",
            None,
            "window frame start at UNBOUNDED FOLLOWING",
        ),
        (
            "SELECT SUM(a) OVER (ORDER BY b ROWS BETWEEN CURRENT ROW AND "
            "UNBOUNDED PRECEDING) AS s FROM t",
            None,
            "window frame end at UNBOUNDED PRECEDING",
        ),
    ],
)
def test_unsupported_offset_function_or_frame(
    sql: str, dialect: str | None, message: str
) -> None:
    assert message in [issue for issue, _ in unsupported_issues(sql, dialect)]


def test_offset_function_value_issues_are_reported() -> None:
    issues = unsupported_issues(
        "SELECT LAG(my_udf(a)) OVER (ORDER BY b) AS l, "
        "LAST_VALUE(my_udf(c)) OVER (ORDER BY b) AS v FROM t"
    )

    assert issues == [
        ("function MY_UDF", "MY_UDF(a)"),
        ("function MY_UDF", "MY_UDF(c)"),
    ]


def test_whole_window_frame_without_order_by_is_the_default() -> None:
    window = window_item(
        "SELECT SUM(a) OVER (PARTITION BY c ROWS BETWEEN UNBOUNDED PRECEDING "
        "AND UNBOUNDED FOLLOWING) AS s FROM t"
    )

    assert window == ir.WindowCall(
        ir.AggregateCall(ir.AggregateFunction.SUM, (A,)), (C,)
    )


@pytest.mark.parametrize(
    ("sql", "node_type", "message"),
    [
        ("SELECT LAG(a) OVER (ORDER BY b) AS v FROM t", exp.Lag, "LAG with EXTRA"),
        (
            "SELECT LAST_VALUE(a) OVER (ORDER BY b) AS v FROM t",
            exp.LastValue,
            "LAST_VALUE with EXTRA",
        ),
        (
            "SELECT NTILE(2) OVER (ORDER BY b) AS v FROM t",
            exp.Ntile,
            "NTILE with EXTRA",
        ),
    ],
)
def test_unknown_parts_of_window_functions_are_rejected(
    sql: str, node_type: type[exp.Expression], message: str
) -> None:
    # Parts a future SQLGlot version may add must never be silently dropped.
    tree = parse_sql(sql)
    tree.find(node_type).set("extra", exp.Literal.number(1))

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    assert [issue.message for issue in caught.value.issues] == [message]


# --- CTEs and subqueries in FROM ---------------------------------------------

T_A = Project(T, (A,))


def test_cte_becomes_a_named_relation() -> None:
    plan = translate_sql("WITH c AS (SELECT a FROM t) SELECT a FROM c")

    assert plan == Project(ir.Named("c", T_A), (A,))


def test_ctes_can_use_earlier_ctes_and_be_used_twice() -> None:
    plan = translate_sql(
        "WITH x AS (SELECT a FROM t), y AS (SELECT a FROM x) "
        "SELECT p.a FROM y p JOIN y q ON p.a = q.a"
    )

    y = ir.Named("y", Project(ir.Named("x", T_A), (A,)))
    assert plan == Project(
        ir.Join(
            ir.RelationAlias(y, "p"),
            ir.RelationAlias(y, "q"),
            ir.JoinKind.INNER,
            BinaryOp(BinaryOperator.EQUAL, Column(("p", "a")), Column(("q", "a"))),
        ),
        (Column(("p", "a")),),
    )


def test_cte_hides_a_table_of_the_same_name_but_not_inside_itself() -> None:
    plan = translate_sql("WITH t AS (SELECT a FROM t WHERE b > 1) SELECT * FROM t")

    filtered = ir.Filter(T, BinaryOp(BinaryOperator.GREATER, B, Literal(1)))
    assert plan == ir.Named("t", Project(filtered, (A,)))


def test_cte_names_are_case_insensitive() -> None:
    plan = translate_sql("WITH Recent AS (SELECT a FROM t) SELECT * FROM RECENT")

    assert plan == ir.Named("Recent", T_A)


def test_schema_qualified_name_is_never_a_cte() -> None:
    plan = translate_sql("WITH t AS (SELECT a FROM u) SELECT * FROM s.t")

    assert plan == TableScan(("s", "t"))


def test_cte_column_list_renames_columns() -> None:
    plan = translate_sql("WITH c (x, y) AS (SELECT a, b FROM t) SELECT x FROM c")

    renamed = ir.RenameColumns(Project(T, (A, B)), ("x", "y"))
    assert plan == Project(ir.Named("c", renamed), (Column(("x",)),))


def test_materialized_cte_is_computed_the_same_way() -> None:
    plan = translate_sql(
        "WITH c AS MATERIALIZED (SELECT a FROM t) SELECT * FROM c", "postgres"
    )

    assert plan == ir.Named("c", T_A)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # Qualified columns need the CTE's name as an alias.
        (
            "WITH c AS (SELECT a FROM t) SELECT c.a FROM c",
            Project(ir.RelationAlias(ir.Named("c", T_A), "c"), (Column(("c", "a")),)),
        ),
        # An explicit alias is always kept.
        (
            "WITH c AS (SELECT a FROM t) SELECT a FROM c AS x",
            Project(ir.RelationAlias(ir.Named("c", T_A), "x"), (A,)),
        ),
        # Unqualified use needs no alias.
        (
            "WITH c AS (SELECT a FROM t) SELECT a FROM c",
            Project(ir.Named("c", T_A), (A,)),
        ),
    ],
)
def test_ctes_are_aliased_only_when_needed(sql: str, expected: ir.Relation) -> None:
    assert translate_sql(sql) == expected


def test_cte_inside_a_subquery_is_not_visible_outside() -> None:
    plan = translate_sql(
        "SELECT s.a FROM (WITH c AS (SELECT a FROM t) SELECT a FROM c) s "
        "JOIN c ON s.a = c.a"
    )

    assert isinstance(plan, Project) and isinstance(plan.source, ir.Join)
    assert plan.source.right == ir.RelationAlias(TableScan(("c",)), "c")


def test_inner_cte_hides_an_outer_one() -> None:
    plan = translate_sql(
        "WITH c AS (SELECT a FROM t) "
        "SELECT * FROM (WITH c AS (SELECT b FROM t) SELECT * FROM c) s"
    )

    assert plan == ir.Named("s", ir.Named("c", Project(T, (B,))))


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        (
            "SELECT s.a FROM (SELECT a FROM t) AS s",
            None,
            Project(ir.RelationAlias(ir.Named("s", T_A), "s"), (Column(("s", "a")),)),
        ),
        ("SELECT a FROM (SELECT a FROM t) s", None, Project(ir.Named("s", T_A), (A,))),
        ("SELECT * FROM (SELECT a FROM t)", "postgres", ir.Named("subquery", T_A)),
        (
            "SELECT y FROM (SELECT a FROM t) s (y)",
            "postgres",
            Project(ir.Named("s", ir.RenameColumns(T_A, ("y",))), (Column(("y",)),)),
        ),
    ],
)
def test_subqueries_in_from(
    sql: str, dialect: str | None, expected: ir.Relation
) -> None:
    assert translate_sql(sql, dialect) == expected


def test_joined_subquery_is_aliased() -> None:
    plan = translate_sql("SELECT * FROM t JOIN (SELECT a FROM t) s ON t.a = s.a")

    assert isinstance(plan, ir.Join)
    assert plan.right == ir.RelationAlias(ir.Named("s", T_A), "s")


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        (
            "WITH RECURSIVE r AS (SELECT a FROM t) SELECT * FROM r",
            "WITH RECURSIVE",
        ),
        (
            "WITH c AS (SELECT a FROM t), C AS (SELECT b FROM t) SELECT * FROM c",
            "CTE name defined twice",
        ),
        (
            "SELECT * FROM (SELECT a FROM t UNION SELECT a FROM u) s",
            "UNION in a subquery",
        ),
        (
            "WITH c AS (SELECT a FROM t UNION SELECT a FROM u) SELECT * FROM c",
            "UNION in a subquery",
        ),
        ("WITH c AS (SELECT my_udf(a) AS x FROM t) SELECT * FROM c", "function MY_UDF"),
    ],
)
def test_unsupported_cte_or_subquery(sql: str, message: str) -> None:
    assert message in [issue for issue, _ in unsupported_issues(sql)]


def test_issues_in_ctes_and_the_main_query_are_reported_together() -> None:
    issues = unsupported_issues(
        "WITH c AS (SELECT my_udf(a) AS x FROM t) SELECT other_udf(x) AS y FROM c"
    )

    assert issues == [
        ("function MY_UDF", "MY_UDF(a)"),
        ("function OTHER_UDF", "OTHER_UDF(x)"),
    ]


def test_nested_select_without_columns_is_a_parse_error() -> None:
    with pytest.raises(SQLParseError, match="SELECT has no columns"):
        translate_sql("SELECT * FROM (SELECT FROM t) s")


@pytest.mark.parametrize(
    ("sql", "find", "part", "message"),
    [
        (
            "WITH c AS (SELECT a FROM t) SELECT * FROM c",
            exp.With,
            "search",
            "WITH SEARCH",
        ),
        (
            "WITH c AS (SELECT a FROM t) SELECT * FROM c",
            exp.CTE,
            "scalar",
            "CTE with SCALAR",
        ),
        (
            "SELECT * FROM (SELECT a FROM t) s",
            exp.Subquery,
            "where",
            "subquery with WHERE",
        ),
    ],
)
def test_unknown_parts_of_ctes_and_subqueries_are_rejected(
    sql: str, find: type[exp.Expression], part: str, message: str
) -> None:
    tree = parse_sql(sql)
    tree.find(find).set(part, exp.true())

    with pytest.raises(UnsupportedSQLError) as caught:
        translate(tree)

    assert message in [issue.message for issue in caught.value.issues]
