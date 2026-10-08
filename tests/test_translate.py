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
        "WITH x AS (SELECT 1) SELECT name FROM customers ORDER BY name"
    )

    assert issues == [
        ("WITH clause", "WITH x AS (SELECT 1)"),
        ("ORDER BY clause", "ORDER BY name"),
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
        ("SELECT a || b AS x FROM t", None, ("DPIPE expression", "a || b")),
        ("SELECT * FROM t GROUP BY a", None, ("SELECT * with aggregation", "*")),
        ("SELECT * FROM t LIMIT 5 OFFSET 2", None, ("OFFSET clause", "OFFSET 2")),
        ("SELECT AS STRUCT * FROM t", "bigquery", ("SELECT AS", "STRUCT")),
        (
            "WITH x AS (SELECT 1) SELECT * FROM x",
            None,
            ("WITH clause", "WITH x AS (SELECT 1)"),
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
            ("FROM source other than a table", "(SELECT 1) AS s"),
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
            "SELECT * FROM a JOIN (SELECT * FROM b) s ON a.id = s.id",
            None,
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
    issues = unsupported_issues("SELECT COUNT(*) OVER () AS n FROM t")

    assert issues == [("WINDOW expression", "COUNT(*) OVER ()")]


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
        ("SELECT CAST(a AS TIMESTAMP) AS x FROM t", None, "CAST to TIMESTAMP"),
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
