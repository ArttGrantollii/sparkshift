"""Equivalence scenarios for GROUP BY, aggregate functions, and HAVING.

Relevant data: order 105 (status 'pending') is the only order with that status
and its amount is NULL; one customer has a NULL country and one order a NULL
status; customer 7 and order 109 are exact duplicate rows.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    # COUNT(*) counts rows; COUNT(col) skips NULLs; duplicates are counted.
    "count rows vs count values": (
        "SELECT COUNT(*) AS all_rows, COUNT(amount) AS with_amount, "
        "COUNT(status) AS with_status FROM orders"
    ),
    # The 'pending' group's only amount is NULL: SUM, AVG, MIN, and MAX are
    # NULL, and COUNT is 0.
    "all-NULL group gives NULL, not zero": (
        "SELECT status, COUNT(amount) AS n, SUM(amount) AS total, AVG(amount) AS mean, "
        "MIN(amount) AS low, MAX(amount) AS high FROM orders GROUP BY status"
    ),
    # Without GROUP BY, an empty input still produces one row.
    "global aggregate over empty input": (
        "SELECT COUNT(*) AS n, SUM(amount) AS total FROM orders WHERE amount > 10000"
    ),
    # With GROUP BY, an empty input produces no rows.
    "grouped aggregate over empty input": (
        "SELECT status, COUNT(*) AS n FROM orders WHERE amount > 10000 GROUP BY status"
    ),
    "NULL keys form one group": (
        "SELECT country, COUNT(*) AS customers FROM customers GROUP BY country"
    ),
    "count distinct ignores NULLs": (
        "SELECT COUNT(DISTINCT country) AS countries, "
        "COUNT(DISTINCT customer_id, country) AS pairs FROM customers"
    ),
    "sum distinct": (
        "SELECT customer_id, SUM(DISTINCT amount) AS distinct_total FROM orders "
        "GROUP BY customer_id"
    ),
    # SUM(decimal(10,2)) is decimal(20,2), AVG(decimal) decimal(14,6), AVG(int) double.
    "result types": (
        "SELECT SUM(amount) AS s, AVG(amount) AS a, SUM(order_id) AS si, "
        "AVG(order_id) AS ai, SUM(discount) AS sd FROM orders"
    ),
    "min and max on strings, dates, timestamps": (
        "SELECT MIN(name) AS first_name, MAX(name) AS last_name, "
        "MIN(signup_date) AS first, MAX(signup_date) AS last FROM customers"
    ),
    "min and max timestamps": (
        "SELECT MIN(created_at) AS first, MAX(created_at) AS last FROM orders"
    ),
    "aggregate expressions and expressions on keys": (
        "SELECT status, SUM(amount) * 2 AS doubled, COUNT(*) + 1 AS n_plus_one, "
        "status = 'completed' AS is_done FROM orders GROUP BY status"
    ),
    "unaliased aggregates keep Spark's names": (
        "SELECT status, COUNT(*), SUM(amount), COUNT(DISTINCT customer_id) "
        "FROM orders GROUP BY status"
    ),
    "HAVING on an aggregate in the SELECT list": (
        "SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id "
        "HAVING COUNT(*) > 1"
    ),
    "HAVING on an aggregate not in the SELECT list": (
        "SELECT customer_id FROM orders GROUP BY customer_id HAVING SUM(amount) > 100"
    ),
    "HAVING on a key": (
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status "
        "HAVING status <> 'completed' OR COUNT(*) > 2"
    ),
    "GROUP BY position": "SELECT country, COUNT(*) AS n FROM customers GROUP BY 1",
    "key aliased in SELECT": (
        "SELECT country AS nation, COUNT(*) AS n FROM customers GROUP BY country"
    ),
    "SELECT order differs from GROUP BY order": (
        "SELECT COUNT(*) AS n, status, customer_id FROM orders "
        "GROUP BY customer_id, status"
    ),
    "key not in the SELECT list": (
        "SELECT COUNT(*) AS n FROM orders GROUP BY customer_id"
    ),
    "group without aggregates": (
        "SELECT customer_id FROM orders GROUP BY customer_id, status"
    ),
    "the spec's headline query": (
        "SELECT c.country, COUNT(*) AS orders, SUM(o.amount) AS revenue "
        "FROM customers c JOIN orders o ON c.customer_id = o.customer_id "
        "WHERE o.status = 'completed' GROUP BY c.country HAVING COUNT(*) > 1"
    ),
    "aggregate then distinct then limit": (
        "SELECT DISTINCT COUNT(*) AS n FROM orders GROUP BY status LIMIT 2"
    ),
}


@pytest.mark.parametrize("sql", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_aggregation_scenario(spark_tables: "SparkSession", sql: str) -> None:
    assert_equivalent(spark_tables, sql)


def test_tsql_aggregation_scenario(spark_tables: "SparkSession") -> None:
    # T-SQL TOP with grouping, checked against hand-written Spark SQL.
    assert_equivalent(
        spark_tables,
        "SELECT TOP 2 customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id",
        dialect="tsql",
        reference_sql=(
            "SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id LIMIT 2"
        ),
    )
