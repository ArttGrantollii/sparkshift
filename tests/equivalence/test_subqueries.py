"""Equivalence scenarios for subqueries in expressions: IN, EXISTS, and
subqueries used as values.

Each scenario is (sql, order_keys): order_keys is None unless the query itself
has ORDER BY.

Relevant data: orders.customer_id has a NULL (order 107) and an unknown
customer 99; customers.customer_id runs 1 to 7 with no NULLs, and customers
5, 6, and 7 have no orders.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    "IN": (
        "SELECT name FROM customers WHERE customer_id IN "
        "(SELECT customer_id FROM orders)",
        None,
    ),
    # The subquery holds a NULL, so "x NOT IN (...)" is never true: no rows.
    # An anti join would return customers 5, 6, and 7.
    "NOT IN with a NULL in the subquery returns nothing": (
        "SELECT name FROM customers WHERE customer_id NOT IN "
        "(SELECT customer_id FROM orders)",
        None,
    ),
    "NOT IN without NULLs": (
        "SELECT name FROM customers WHERE customer_id NOT IN "
        "(SELECT customer_id FROM orders WHERE customer_id IS NOT NULL)",
        None,
    ),
    # Order 107 has no customer: NULL NOT IN (...) is not true, so it is
    # excluded. An anti join would keep it.
    "NOT IN with a NULL on the left": (
        "SELECT order_id FROM orders WHERE customer_id NOT IN "
        "(SELECT customer_id FROM customers)",
        None,
    ),
    "IN among other conditions": (
        "SELECT order_id FROM orders WHERE amount > 50 AND customer_id IN "
        "(SELECT customer_id FROM customers WHERE country = 'US')",
        None,
    ),
    "correlated EXISTS": (
        "SELECT c.name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o "
        "WHERE o.customer_id = c.customer_id AND o.status = 'completed')",
        None,
    ),
    "correlated NOT EXISTS": (
        "SELECT c.customer_id, c.name FROM customers c WHERE NOT EXISTS "
        "(SELECT 1 FROM orders o WHERE o.customer_id = c.customer_id)",
        None,
    ),
    "uncorrelated EXISTS": (
        "SELECT name FROM customers WHERE EXISTS "
        "(SELECT 1 FROM orders WHERE amount > 900)",
        None,
    ),
    "subquery as a value in WHERE": (
        "SELECT order_id, amount FROM orders WHERE amount > "
        "(SELECT AVG(amount) FROM orders)",
        None,
    ),
    "correlated subquery as a value in SELECT": (
        "SELECT o.order_id, (SELECT c.name FROM customers c "
        "WHERE c.customer_id = o.customer_id) AS buyer FROM orders o",
        None,
    ),
    # Customers without orders count 0, not NULL.
    "correlated COUNT in SELECT": (
        "SELECT c.customer_id, (SELECT COUNT(*) FROM orders o "
        "WHERE o.customer_id = c.customer_id) AS order_count FROM customers c",
        None,
    ),
    "EXISTS in SELECT": (
        "SELECT c.customer_id, EXISTS (SELECT 1 FROM orders o "
        "WHERE o.customer_id = c.customer_id) AS has_orders FROM customers c",
        None,
    ),
    "subquery as a value in HAVING": (
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status "
        "HAVING COUNT(*) >= (SELECT COUNT(*) FROM customers WHERE country = 'DE')",
        None,
    ),
    "subquery as a value in ORDER BY": (
        "SELECT order_id FROM orders "
        "ORDER BY ABS(amount - (SELECT AVG(amount) FROM orders)), order_id",
        ["order_id"],
    ),
    "subquery using a CTE": (
        "WITH us AS (SELECT customer_id FROM customers WHERE country = 'US') "
        "SELECT order_id FROM orders WHERE customer_id IN (SELECT customer_id FROM us)",
        None,
    ),
    "subquery inside a subquery": (
        "SELECT name FROM customers WHERE customer_id IN (SELECT customer_id "
        "FROM orders WHERE amount > (SELECT AVG(amount) FROM orders))",
        None,
    ),
}


@pytest.mark.parametrize(
    ("sql", "order_keys"), SCENARIOS.values(), ids=SCENARIOS.keys()
)
def test_subquery_scenario(
    spark_tables: "SparkSession", sql: str, order_keys: list[str] | None
) -> None:
    assert_equivalent(spark_tables, sql, order_keys=order_keys)


DIALECT_SCENARIOS = {
    "T-SQL TOP with IN": (
        "tsql",
        "SELECT TOP 2 name FROM customers WHERE customer_id IN "
        "(SELECT customer_id FROM orders) ORDER BY name",
        "SELECT name FROM customers WHERE customer_id IN "
        "(SELECT customer_id FROM orders) ORDER BY name LIMIT 2",
        ["name"],
    ),
    "PostgreSQL NOT EXISTS, ordered with NULLs last": (
        "postgres",
        "SELECT c.name FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o "
        "WHERE o.customer_id = c.customer_id) ORDER BY c.name",
        "SELECT c.name FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o "
        "WHERE o.customer_id = c.customer_id) ORDER BY c.name NULLS LAST",
        ["name"],
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql", "order_keys"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_subquery_scenario(
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
