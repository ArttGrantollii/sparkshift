"""Equivalence scenarios for CTEs (WITH) and subqueries in FROM.

Each scenario is (sql, order_keys): order_keys is None unless the query itself
has ORDER BY.

Relevant data: customer totals are 200.50 (customer 1), 160.00 (2), -15.75
(3), 999.99 (4), 42.00 (no customer), and 10.00 (unknown customer 99); order
106 has a NULL status.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    "single CTE": (
        "WITH completed AS (SELECT order_id, customer_id, amount FROM orders "
        "WHERE status = 'completed') "
        "SELECT customer_id, SUM(amount) AS total FROM completed GROUP BY customer_id",
        None,
    ),
    "CTE using an earlier CTE": (
        "WITH paid AS (SELECT customer_id, amount FROM orders WHERE amount > 0), "
        "totals AS (SELECT customer_id, SUM(amount) AS total FROM paid "
        "GROUP BY customer_id) "
        "SELECT customer_id, total FROM totals WHERE total > 100",
        None,
    ),
    "CTE used twice, in a self-join": (
        "WITH spend AS (SELECT customer_id, SUM(amount) AS total FROM orders "
        "GROUP BY customer_id) "
        "SELECT a.customer_id, b.customer_id AS bigger_spender "
        "FROM spend a JOIN spend b ON a.total < b.total",
        None,
    ),
    # Inside the CTE, orders is the table; after it, orders is the CTE.
    "CTE hiding a table of the same name": (
        "WITH orders AS (SELECT order_id, amount FROM orders WHERE amount > 50) "
        "SELECT * FROM orders",
        None,
    ),
    "CTE column list": (
        "WITH totals (customer, spent) AS (SELECT customer_id, SUM(amount) "
        "FROM orders GROUP BY customer_id) SELECT customer, spent FROM totals",
        None,
    ),
    "CTE joined to a table with qualified columns": (
        "WITH big AS (SELECT order_id, customer_id FROM orders WHERE amount >= 80) "
        "SELECT c.name, big.order_id FROM customers c "
        "JOIN big ON c.customer_id = big.customer_id",
        None,
    ),
    # The first order of each customer: a window in a CTE, filtered outside.
    "window in a CTE, filtered outside": (
        "WITH numbered AS (SELECT order_id, customer_id, "
        "ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id) AS nth "
        "FROM orders) "
        "SELECT order_id, customer_id FROM numbered WHERE nth = 1",
        None,
    ),
    "aggregation inside and outside a subquery": (
        "SELECT COUNT(*) AS big_customers FROM (SELECT customer_id, "
        "SUM(amount) AS total FROM orders GROUP BY customer_id) s "
        "WHERE s.total > 100",
        None,
    ),
    # The NULL status group is not joined: NULL = NULL is not true.
    "two subqueries joined": (
        "SELECT a.status, a.n, b.total FROM "
        "(SELECT status, COUNT(*) AS n FROM orders GROUP BY status) a "
        "JOIN (SELECT status, SUM(amount) AS total FROM orders GROUP BY status) b "
        "ON a.status = b.status",
        None,
    ),
    # The two largest amounts are distinct, so the LIMIT keeps fixed rows.
    "top-N in a subquery, ordered outside": (
        "SELECT * FROM (SELECT order_id, amount FROM orders "
        "ORDER BY amount DESC LIMIT 2) best ORDER BY order_id",
        ["order_id"],
    ),
    "CTE inside a subquery": (
        "SELECT n FROM (WITH c AS (SELECT customer_id FROM orders WHERE amount > 50) "
        "SELECT COUNT(*) AS n FROM c) s",
        None,
    ),
    "CTE used in a subquery": (
        "WITH completed AS (SELECT customer_id, amount FROM orders "
        "WHERE status = 'completed') "
        "SELECT s.customer_id FROM (SELECT customer_id, MAX(amount) AS top "
        "FROM completed GROUP BY customer_id) s WHERE s.top >= 80",
        None,
    ),
}


@pytest.mark.parametrize(
    ("sql", "order_keys"), SCENARIOS.values(), ids=SCENARIOS.keys()
)
def test_cte_scenario(
    spark_tables: "SparkSession", sql: str, order_keys: list[str] | None
) -> None:
    assert_equivalent(spark_tables, sql, order_keys=order_keys)


DIALECT_SCENARIOS = {
    "T-SQL TOP over a CTE": (
        "tsql",
        "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM orders "
        "GROUP BY customer_id) "
        "SELECT TOP 2 customer_id, total FROM totals ORDER BY total DESC",
        "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM orders "
        "GROUP BY customer_id) "
        "SELECT customer_id, total FROM totals ORDER BY total DESC LIMIT 2",
        ["total"],
    ),
    "PostgreSQL subquery column list": (
        "postgres",
        "SELECT spent FROM (SELECT customer_id, SUM(amount) FROM orders "
        "GROUP BY customer_id) s (customer, spent)",
        "SELECT spent FROM (SELECT customer_id AS customer, SUM(amount) AS spent "
        "FROM orders GROUP BY customer_id) s",
        None,
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql", "order_keys"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_cte_scenario(
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
