import pytest

from sparkshift.errors import SQLParseError, UnsupportedSQLError
from sparkshift.ir import TableScan
from sparkshift.parsing import parse_sql
from sparkshift.translate import translate


def translate_sql(sql: str, dialect: str | None = None):
    return translate(parse_sql(sql, dialect), dialect)


def unsupported_issues(sql: str, dialect: str | None = None) -> list[tuple[str, str]]:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate_sql(sql, dialect)
    return [(issue.message, issue.sql) for issue in caught.value.issues]


# --- Supported ---------------------------------------------------------------


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


# --- Unsupported: every construct is reported --------------------------------


def test_all_unsupported_constructs_are_reported_together() -> None:
    issues = unsupported_issues(
        "SELECT name FROM customers WHERE age > 30 ORDER BY name"
    )

    assert issues == [
        ("column list", "name"),
        ("WHERE clause", "WHERE age > 30"),
        ("ORDER BY clause", "ORDER BY name"),
    ]


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        ("SELECT a, b FROM t", None, ("column list", "a, b")),
        ("SELECT t.* FROM t", None, ("column list", "t.*")),
        ("SELECT *, 1 FROM t", None, ("column list", "*, 1")),
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
