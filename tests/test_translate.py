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
        "SELECT name FROM customers WHERE age > 30 ORDER BY name"
    )

    assert issues == [
        ("WHERE clause", "WHERE age > 30"),
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
        ("SELECT t.* FROM t", None, ("qualified star", "t.*")),
        (
            "SELECT CAST(a AS INT) AS x FROM t",
            None,
            ("function CAST", "CAST(a AS INT)"),
        ),
        ("SELECT a IS NULL AS x FROM t", None, ("IS expression", "a IS NULL")),
        ("SELECT a LIKE 'x%' AS x FROM t", None, ("LIKE expression", "a LIKE 'x%'")),
        ("SELECT a || b AS x FROM t", None, ("DPIPE expression", "a || b")),
        ("SELECT DISTINCT * FROM t", None, ("DISTINCT", "DISTINCT")),
        ("SELECT * FROM t GROUP BY a", None, ("GROUP BY clause", "GROUP BY a")),
        ("SELECT * FROM t LIMIT 5", None, ("LIMIT clause", "LIMIT 5")),
        # Fragments are regenerated from the AST, not copied from the input, so
        # they show SQLGlot's normalized form: TOP 5 is stored as a Limit node.
        ("SELECT TOP 5 * FROM t", "tsql", ("LIMIT clause", "LIMIT 5")),
        ("SELECT AS STRUCT * FROM t", "bigquery", ("SELECT AS", "STRUCT")),
        (
            "WITH x AS (SELECT 1) SELECT * FROM x",
            None,
            ("WITH clause", "WITH x AS (SELECT 1)"),
        ),
        ("SELECT * FROM t1, t2", None, ("JOIN", ", t2")),
        ("SELECT * FROM customers c", None, ("table alias", "customers AS c")),
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


def test_each_join_is_reported_separately() -> None:
    issues = unsupported_issues(
        "SELECT * FROM a JOIN b ON a.id = b.id LEFT JOIN c ON c.id = a.id"
    )

    assert issues == [
        ("JOIN", "JOIN b ON a.id = b.id"),
        ("JOIN", "LEFT JOIN c ON c.id = a.id"),
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
