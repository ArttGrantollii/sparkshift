"""Tests for the public API, used exactly as a library user would."""

import pytest

import sparkshift

UNSUPPORTED_EXAMPLE = (
    "SELECT name, my_udf(score) AS s FROM customers LIMIT 10 OFFSET 20"
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
        "2 unsupported constructs:\n"
        "  - function MY_UDF: MY_UDF(score)\n"
        "  - OFFSET clause: OFFSET 20"
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
        # Too long for one line: the condition moves into its own parentheses.
        "        (\n"
        '            (F.col("amount") > F.lit(100)) & '
        '(F.col("status") == F.lit("completed"))\n'
        '        ).alias("big_sale"),\n'
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
        ("data a; set b", "sas", sparkshift.SASParseError),
        ("data a; set b; total = 1; run;", "sas", sparkshift.UnsupportedSQLError),
    ],
)
def test_every_failure_is_a_sparkshift_error(
    sql: str, dialect: str | None, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type) as caught:
        sparkshift.convert(sql, dialect=dialect)

    assert isinstance(caught.value, sparkshift.SparkShiftError)


def test_convert_a_sas_program() -> None:
    result = sparkshift.convert(
        "data us; set sales.customers(keep=id name); where country = 'US'; run;",
        dialect="SAS",
    )

    assert result.code == (
        "from pyspark.sql import functions as F\n"
        "\n"
        "us = (\n"
        '    spark.table("sales.customers")\n'
        "    .select(\n"
        '        F.col("id"),\n'
        '        F.col("name"),\n'
        "    )\n"
        '    .where(F.coalesce(F.rtrim(F.col("country")), F.lit("")) == F.lit("US"))\n'
        ")\n"
        "\n"
        "result = us\n"
    )
    assert [warning.message for warning in result.warnings] == [
        "KEEP of several variables"
    ]


def test_a_sas_parse_error_is_an_sql_parse_error() -> None:
    # Callers that handle invalid input handle both languages.
    assert issubclass(sparkshift.SASParseError, sparkshift.SQLParseError)
