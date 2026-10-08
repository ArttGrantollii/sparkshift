"""Tests for the public API, used exactly as a library user would."""

import pytest

import sparkshift

UNSUPPORTED_EXAMPLE = (
    "SELECT country, COUNT(*) AS n FROM customers GROUP BY country ORDER BY n"
)


def test_convert_returns_code_and_no_warnings() -> None:
    result = sparkshift.convert("SELECT * FROM customers")

    assert isinstance(result, sparkshift.ConversionResult)
    assert result.code == 'result = spark.table("customers")\n'
    assert result.warnings == ()


@pytest.mark.parametrize(
    ("sql", "dialect", "expected_code"),
    [
        ("SELECT * FROM `my table`", "mysql", 'result = spark.table("`my table`")\n'),
        ("SELECT * FROM [my table]", "tsql", 'result = spark.table("`my table`")\n'),
        ("SELECT * FROM [my table]", "TSQL", 'result = spark.table("`my table`")\n'),
        (
            "SELECT * FROM `proj.dataset.customers`",
            "bigquery",
            'result = spark.table("proj.dataset.customers")\n',
        ),
    ],
)
def test_convert_honors_the_dialect(sql: str, dialect: str, expected_code: str) -> None:
    assert sparkshift.convert(sql, dialect=dialect).code == expected_code


def test_unsupported_query_lists_every_issue() -> None:
    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        sparkshift.convert(UNSUPPORTED_EXAMPLE)

    assert str(caught.value) == (
        "3 unsupported constructs:\n"
        "  - function COUNT: COUNT(*)\n"
        "  - GROUP BY clause: GROUP BY country\n"
        "  - ORDER BY clause: ORDER BY n"
    )


def test_convert_generates_a_projection() -> None:
    sql = (
        "SELECT order_id, amount * 1.10 AS with_tax, "
        "amount > 100 AND status = 'completed' AS big_sale FROM orders"
    )

    assert sparkshift.convert(sql).code == (
        "from decimal import Decimal\n"
        "\n"
        "from pyspark.sql import functions as F\n"
        "\n"
        "result = (\n"
        '    spark.table("orders")\n'
        "    .select(\n"
        '        F.col("order_id"),\n'
        '        (F.col("amount") * F.lit(Decimal("1.10"))).alias("with_tax"),\n'
        # Long expressions are not wrapped yet; this line is 99 characters.
        '        ((F.col("amount") > F.lit(100)) & '
        '(F.col("status") == F.lit("completed"))).alias("big_sale"),\n'
        "    )\n"
        ")\n"
    )


@pytest.mark.parametrize(
    ("sql", "dialect", "error_type"),
    [
        ("SELECT * FROM t", "sqlserver", sparkshift.UnsupportedDialectError),
        ("SELECT (a FROM t", None, sparkshift.SQLParseError),
        ("SELECT 1; SELECT 2", None, sparkshift.MultipleStatementsError),
        ("DELETE FROM t", None, sparkshift.UnsupportedSQLError),
    ],
)
def test_every_failure_is_a_sparkshift_error(
    sql: str, dialect: str | None, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type) as caught:
        sparkshift.convert(sql, dialect=dialect)

    assert isinstance(caught.value, sparkshift.SparkShiftError)
