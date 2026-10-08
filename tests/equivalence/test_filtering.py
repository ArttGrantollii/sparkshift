"""Equivalence scenarios for WHERE, DISTINCT, and row limits.

Each scenario names the semantic risk it guards against.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    # WHERE keeps a row only when the condition is TRUE; NULL drops it.
    "comparison drops NULL rows": "SELECT * FROM orders WHERE status = 'completed'",
    "inequality drops NULL rows": "SELECT * FROM customers WHERE country <> 'US'",
    "boolean column": "SELECT * FROM customers WHERE is_active",
    # The customer with a NULL is_active appears in neither this nor the above.
    "NOT on a NULL boolean": "SELECT * FROM customers WHERE NOT is_active",
    "three-valued OR": "SELECT * FROM customers WHERE is_active OR score > 4",
    "filter on a column that is not selected": (
        "SELECT order_id FROM orders WHERE amount >= 80.00 AND discount > 0"
    ),
    "empty string is not NULL": "SELECT * FROM customers WHERE name = ''",
    # WHERE is evaluated before SELECT, so it sees the source column, not the
    # SELECT alias that reuses its name. Filtering after projecting would see
    # the doubled values and keep different rows.
    "WHERE sees source columns, not same-named aliases": (
        "SELECT order_id, amount * 2 AS amount FROM orders WHERE amount > 100"
    ),
    # DISTINCT treats NULLs as equal to each other.
    "distinct removes duplicate rows": "SELECT DISTINCT * FROM customers",
    "distinct groups NULLs together": "SELECT DISTINCT country FROM customers",
    "filter then distinct": (
        "SELECT DISTINCT customer_id, status FROM orders WHERE amount > 0"
    ),
    # LIMIT without ORDER BY may return any rows; checked as a sub-multiset.
    "limit": "SELECT * FROM orders LIMIT 3",
    "limit zero": "SELECT * FROM orders LIMIT 0",
    "limit larger than the table": "SELECT * FROM customers LIMIT 100",
    "distinct then limit": "SELECT DISTINCT country FROM customers LIMIT 2",
}


@pytest.mark.parametrize("sql", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_filtering_scenario(spark_tables: "SparkSession", sql: str) -> None:
    assert_equivalent(spark_tables, sql)


# Dialect syntax Spark cannot run directly, checked against hand-written Spark SQL.
DIALECT_SCENARIOS = {
    "T-SQL TOP": (
        "tsql",
        "SELECT TOP 3 order_id, status FROM orders WHERE status = 'completed'",
        "SELECT order_id, status FROM orders WHERE status = 'completed' LIMIT 3",
    ),
    "Oracle FETCH FIRST": (
        "oracle",
        "SELECT order_id FROM orders WHERE amount > 50 FETCH FIRST 2 ROWS ONLY",
        "SELECT order_id FROM orders WHERE amount > 50 LIMIT 2",
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_row_limit_scenario(
    spark_tables: "SparkSession", dialect: str, sql: str, reference_sql: str
) -> None:
    assert_equivalent(spark_tables, sql, dialect=dialect, reference_sql=reference_sql)
