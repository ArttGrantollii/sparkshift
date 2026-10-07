import pytest
from sqlglot import exp
from sqlglot.errors import ParseError

from sparkshift.errors import (
    MultipleStatementsError,
    SparkShiftError,
    SQLParseError,
    UnsupportedDialectError,
)
from sparkshift.parsing import _to_sql_parse_error, parse_sql

# --- Valid input -------------------------------------------------------------


def test_parses_a_query_into_a_syntax_tree() -> None:
    tree = parse_sql("SELECT name FROM customers WHERE age > 30")

    # Select
    # ├── expressions: [Column(name)]
    # ├── from_:       From(Table(customers))
    # └── where:       Where(GT(Column(age), Literal(30)))
    assert isinstance(tree, exp.Select)
    assert [column.name for column in tree.expressions] == ["name"]
    assert tree.args["from_"].this.name == "customers"

    condition = tree.args["where"].this
    assert isinstance(condition, exp.GT)
    assert condition.this.name == "age"
    assert condition.expression.this == "30"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1;",
        "; SELECT 1",
        "SELECT 1;;",
        # SQLGlot returns an extra Semicolon node holding this trailing comment.
        "SELECT 1; -- trailing comment",
    ],
)
def test_stray_semicolons_and_trailing_comments_still_count_as_one_statement(
    sql: str,
) -> None:
    assert isinstance(parse_sql(sql), exp.Select)


def test_lenient_grammar_is_not_validation() -> None:
    # SQLGlot accepts a SELECT with no columns. Parsing succeeding does not mean
    # the query is valid; the translator must reject constructs like this itself.
    tree = parse_sql("SELECT FROM t")

    assert isinstance(tree, exp.Select)
    assert tree.expressions == []


# --- Dialects ----------------------------------------------------------------

# Syntax that only the named dialect understands: the generic dialect rejects it.
DIALECT_ONLY_SYNTAX = [
    ("tsql", "SELECT TOP 5 name FROM customers"),
    ("postgres", "SELECT a FROM t WHERE b ~ 'x'"),
    ("mysql", "SELECT `order id` FROM `my table`"),
    ("snowflake", "SELECT v:name::string AS n FROM t"),
    ("bigquery", "SELECT name FROM `proj.dataset.customers`"),
]


@pytest.mark.parametrize(("dialect", "sql"), DIALECT_ONLY_SYNTAX)
def test_dialect_specific_syntax_needs_its_dialect(dialect: str, sql: str) -> None:
    assert isinstance(parse_sql(sql, dialect=dialect), exp.Select)

    with pytest.raises(SQLParseError):
        parse_sql(sql)


def test_oracle_outer_join_operator_parses() -> None:
    # Oracle's (+) syntax is also accepted by the generic dialect, so this only
    # checks that the oracle dialect is wired through correctly.
    tree = parse_sql("SELECT a FROM t1, t2 WHERE t1.id = t2.id(+)", dialect="oracle")

    assert isinstance(tree, exp.Select)


def test_same_text_can_produce_different_trees_in_different_dialects() -> None:
    sql = "SELECT name FROM `proj.dataset.customers`"

    mysql_table = parse_sql(sql, dialect="mysql").find(exp.Table)
    bigquery_table = parse_sql(sql, dialect="bigquery").find(exp.Table)

    # MySQL: one quoted identifier containing dots.
    assert mysql_table is not None
    assert (mysql_table.catalog, mysql_table.db, mysql_table.name) == (
        "",
        "",
        "proj.dataset.customers",
    )
    # BigQuery: project, dataset, and table.
    assert bigquery_table is not None
    assert (bigquery_table.catalog, bigquery_table.db, bigquery_table.name) == (
        "proj",
        "dataset",
        "customers",
    )


def test_unknown_dialect_is_rejected_before_parsing() -> None:
    with pytest.raises(UnsupportedDialectError):
        parse_sql("SELECT 1", dialect="sqlserver")


# --- Invalid input -----------------------------------------------------------


@pytest.mark.parametrize(
    "sql", ["", "   ", ";", "-- only a comment", "/* only a comment */"]
)
def test_input_without_a_statement_is_rejected(sql: str) -> None:
    with pytest.raises(SQLParseError, match="No SQL statement found"):
        parse_sql(sql)


def test_multiple_statements_are_rejected() -> None:
    with pytest.raises(MultipleStatementsError) as caught:
        parse_sql("SELECT 1; SELECT 2")

    assert caught.value.count == 2


def test_parse_error_reports_where_the_problem_starts() -> None:
    with pytest.raises(SQLParseError) as caught:
        parse_sql("SELECT (a FROM t")

    error = caught.value
    assert error.message == "Expecting ) near 'FROM'"
    # "FROM" occupies columns 11-14; the error points at its start.
    assert (error.line, error.column) == (1, 11)
    assert str(error) == "Expecting ) near 'FROM' (line 1, column 11)"


def test_parse_error_reports_the_line_in_multiline_sql() -> None:
    sql = "SELECT a,\n       b\nFROM t\nWHERE (x = 1"

    with pytest.raises(SQLParseError) as caught:
        parse_sql(sql)

    assert (caught.value.line, caught.value.column) == (4, 12)


@pytest.mark.parametrize(
    ("sql", "expected_message"),
    [
        ("SELECT a FROM", "Expected table name but got end of input near 'FROM'"),
        ("SELECT a FROM )", "Expected table name but got ')' near ')'"),
        ("SELECT a FROM t WHERE", "Incomplete WHERE near 'WHERE'"),
        ("SELECT * FROM t LIMIT", "Incomplete LIMIT near 'LIMIT'"),
    ],
)
def test_parse_error_messages_are_readable(sql: str, expected_message: str) -> None:
    with pytest.raises(SQLParseError) as caught:
        parse_sql(sql)

    assert caught.value.message == expected_message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM",
        "SELECT a FROM )",
        "SELECT a FROM t WHERE",
        "SELECT a FROM t JOIN",
        "SELECT * FROM t LIMIT",
        "SELECT a FROM (SELECT",
        "SELECT CAST(a AS) FROM t",
        "SELEC a FROM t",
    ],
)
def test_parse_error_messages_hide_sqlglot_internals(sql: str) -> None:
    with pytest.raises(SQLParseError) as caught:
        parse_sql(sql)

    message = str(caught.value)
    for internal in ("<Token", "<class", "sqlglot", "TokenType", "\x1b"):
        assert internal not in message


def test_unterminated_string_is_a_parse_error_without_position() -> None:
    with pytest.raises(SQLParseError) as caught:
        parse_sql("SELECT 'abc FROM t")

    assert "unterminated string" in caught.value.message
    assert caught.value.line is None
    assert caught.value.column is None


def test_parse_error_without_details_still_converts() -> None:
    error = _to_sql_parse_error(ParseError("internal failure"))

    assert error.message == "Invalid SQL."
    assert error.line is None


@pytest.mark.parametrize(
    "sql",
    ["SELECT (a FROM t", "SELECT 'abc FROM t", "", "SELECT 1; SELECT 2"],
)
def test_every_failure_is_a_sparkshift_error(sql: str) -> None:
    with pytest.raises(SparkShiftError):
        parse_sql(sql)
