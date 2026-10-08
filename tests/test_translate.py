from decimal import Decimal

import pytest

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
        "SELECT name FROM customers GROUP BY name ORDER BY name"
    )

    assert issues == [
        ("GROUP BY clause", "GROUP BY name"),
        ("ORDER BY clause", "ORDER BY name"),
    ]


def test_issues_inside_one_expression_are_all_reported() -> None:
    issues = unsupported_issues("SELECT COUNT(a) + my_udf(b) AS x FROM t")

    assert issues == [("function COUNT", "COUNT(a)"), ("function MY_UDF", "MY_UDF(b)")]


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        ("SELECT -amount FROM t", None, ("negation without an alias", "-amount")),
        ("SELECT -COUNT(a) AS x FROM t", None, ("function COUNT", "COUNT(a)")),
        ("SELECT COUNT(t.*) AS n FROM t", None, ("function COUNT", "COUNT(t.*)")),
        (
            "SELECT CAST(a AS INT) AS x FROM t",
            None,
            ("function CAST", "CAST(a AS INT)"),
        ),
        ("SELECT a IS NULL AS x FROM t", None, ("IS expression", "a IS NULL")),
        ("SELECT a LIKE 'x%' AS x FROM t", None, ("LIKE expression", "a LIKE 'x%'")),
        ("SELECT a || b AS x FROM t", None, ("DPIPE expression", "a || b")),
        ("SELECT * FROM t GROUP BY a", None, ("GROUP BY clause", "GROUP BY a")),
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
        ("SELECT * FROM t WHERE COUNT(a) > 1", None, "function COUNT"),
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
        ("SELECT * FROM a JOIN b ON a.id = COUNT(b.id)", None, "function COUNT"),
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
