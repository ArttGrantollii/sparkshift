"""Equivalence scenarios for window functions.

ROW_NUMBER over tied rows may number them in any order, in every database. So
where a window's order decides a result, these scenarios order by keys whose
only ties are identical rows (the duplicates of order 109 and customer 7),
which give the same rows whichever way they are numbered.

Each scenario is (sql, order_keys): order_keys is None unless the query itself
has ORDER BY.

Relevant data: order 105 has a NULL amount; order 106 a NULL status; order 107
a NULL customer; six orders are 'completed', three of them at 80.00; customer
4 has a NULL country and customer 3 a NULL score.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    # The NULL customer forms its own partition.
    "row number per partition": (
        "SELECT order_id, customer_id, "
        "ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id) AS nth "
        "FROM orders",
        None,
    ),
    "rank and dense rank over ties": (
        "SELECT order_id, amount, RANK() OVER (ORDER BY amount DESC) AS r, "
        "DENSE_RANK() OVER (ORDER BY amount DESC) AS d FROM orders",
        None,
    ),
    # With ORDER BY, the default frame ends at the current row's last tie:
    # all six 'completed' rows get the same running total.
    "running total shared by tied rows": (
        "SELECT order_id, status, SUM(amount) OVER (ORDER BY status) AS running "
        "FROM orders",
        None,
    ),
    # Without ORDER BY, the frame is the whole partition. The 'pending'
    # partition's only amount is NULL, so its SUM is NULL.
    "partition totals": (
        "SELECT order_id, status, SUM(amount) OVER (PARTITION BY status) AS total, "
        "COUNT(*) OVER (PARTITION BY status) AS orders_in_status FROM orders",
        None,
    ),
    "whole result as one window": (
        "SELECT order_id, COUNT(*) OVER () AS all_orders, "
        "MAX(amount) OVER () AS largest FROM orders",
        None,
    ),
    "average, minimum, and maximum skip NULLs": (
        "SELECT order_id, customer_id, AVG(amount) OVER (PARTITION BY customer_id) "
        "AS mean, MIN(amount) OVER (PARTITION BY customer_id) AS low, "
        "MAX(amount) OVER (PARTITION BY customer_id) AS high, "
        "COUNT(amount) OVER (PARTITION BY customer_id) AS with_amount FROM orders",
        None,
    ),
    "windows see only rows that pass WHERE": (
        "SELECT order_id, ROW_NUMBER() OVER (ORDER BY order_id) AS nth FROM orders "
        "WHERE status = 'completed'",
        None,
    ),
    # Ranks 1 and 2, then three rows tied at rank 3: the LIMIT keeps one.
    "ORDER BY a window alias with LIMIT": (
        "SELECT order_id, amount, RANK() OVER (ORDER BY amount DESC) AS r "
        "FROM orders ORDER BY r LIMIT 3",
        ["r"],
    ),
    "several different windows": (
        "SELECT order_id, "
        "ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id) AS nth, "
        "SUM(amount) OVER (PARTITION BY status) AS status_total FROM orders",
        None,
    ),
    "several sort keys and a NULL partition": (
        "SELECT customer_id, "
        "DENSE_RANK() OVER (PARTITION BY country ORDER BY score DESC, name) AS place "
        "FROM customers",
        None,
    ),
    # Orders span January to March 2024: three partitions.
    "expressions in PARTITION BY and ORDER BY": (
        "SELECT order_id, RANK() OVER (PARTITION BY MONTH(order_date) "
        "ORDER BY amount * -1) AS r FROM orders",
        None,
    ),
    "DISTINCT after a window": (
        "SELECT DISTINCT status, COUNT(*) OVER (PARTITION BY status) AS n FROM orders",
        None,
    ),
    "window over a join": (
        "SELECT c.name, o.order_id, "
        "ROW_NUMBER() OVER (PARTITION BY c.customer_id ORDER BY o.order_id) AS nth "
        "FROM customers c JOIN orders o ON c.customer_id = o.customer_id",
        None,
    ),
}


@pytest.mark.parametrize(
    ("sql", "order_keys"), SCENARIOS.values(), ids=SCENARIOS.keys()
)
def test_window_scenario(
    spark_tables: "SparkSession", sql: str, order_keys: list[str] | None
) -> None:
    assert_equivalent(spark_tables, sql, order_keys=order_keys)


# Each dialect's NULL placement inside the window, spelled out in Spark SQL.
DIALECT_SCENARIOS = {
    "PostgreSQL rank puts NULLs last": (
        "postgres",
        "SELECT order_id, RANK() OVER (ORDER BY amount) AS r FROM orders",
        "SELECT order_id, RANK() OVER (ORDER BY amount NULLS LAST) AS r FROM orders",
        None,
    ),
    # The NULL amount comes first, so its running total is NULL.
    "Oracle running total puts NULLs first when descending": (
        "oracle",
        "SELECT order_id, SUM(amount) OVER (ORDER BY amount DESC) AS running "
        "FROM orders",
        "SELECT order_id, SUM(amount) OVER (ORDER BY amount DESC NULLS FIRST) "
        "AS running FROM orders",
        None,
    ),
    "Snowflake partitioned dense rank": (
        "snowflake",
        "SELECT order_id, "
        "DENSE_RANK() OVER (PARTITION BY customer_id ORDER BY amount) AS d "
        "FROM orders",
        "SELECT order_id, "
        "DENSE_RANK() OVER (PARTITION BY customer_id ORDER BY amount NULLS LAST) "
        "AS d FROM orders",
        None,
    ),
    "MySQL puts NULLs first, like Spark": (
        "mysql",
        "SELECT customer_id, RANK() OVER (ORDER BY name) AS r FROM customers",
        "SELECT customer_id, RANK() OVER (ORDER BY name NULLS FIRST) AS r "
        "FROM customers",
        None,
    ),
    "T-SQL TOP with a window": (
        "tsql",
        "SELECT TOP 3 order_id, ROW_NUMBER() OVER (ORDER BY order_id DESC) AS nth "
        "FROM orders ORDER BY nth",
        "SELECT order_id, ROW_NUMBER() OVER (ORDER BY order_id DESC) AS nth "
        "FROM orders ORDER BY nth LIMIT 3",
        ["nth"],
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql", "order_keys"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_window_scenario(
    spark_tables: "SparkSession",
    dialect: str,
    sql: str,
    reference_sql: str,
    order_keys: list[str] | None,
) -> None:
    assert_equivalent(
        spark_tables,
        sql,
        dialect=dialect,
        reference_sql=reference_sql,
        order_keys=order_keys,
    )
