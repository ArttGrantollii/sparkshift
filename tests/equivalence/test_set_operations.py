"""Equivalence scenarios for UNION, INTERSECT, and EXCEPT.

Each scenario is (sql, order_keys): order_keys is None unless the query itself
has ORDER BY.

Relevant data: orders.customer_id holds 1 twice, 2 three times, 3 twice, 4,
99, and NULL; customers.customer_id holds 1 to 6 once and 7 twice. So the
distinct and ALL forms of each operator give different answers.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

ORDER_IDS = "SELECT customer_id FROM orders"
CUSTOMER_IDS = "SELECT customer_id FROM customers"

SCENARIOS = {
    # Duplicates removed, across and within the inputs; NULL counts once.
    "UNION": (f"{ORDER_IDS} UNION {CUSTOMER_IDS}", None),
    "UNION ALL keeps duplicates": (f"{ORDER_IDS} UNION ALL {CUSTOMER_IDS}", None),
    # Customers 1, 2, and 4 have orders of 80 or more: 1 and 2 twice each.
    # INTERSECT returns each once; INTERSECT ALL keeps the shared counts.
    "INTERSECT removes duplicates": (
        f"{ORDER_IDS} INTERSECT {ORDER_IDS} WHERE amount >= 80",
        None,
    ),
    "INTERSECT ALL counts duplicates": (
        f"{ORDER_IDS} INTERSECT ALL {ORDER_IDS} WHERE amount >= 80",
        None,
    ),
    "EXCEPT": (f"{CUSTOMER_IDS} EXCEPT {ORDER_IDS}", None),
    # Each customer row cancels one order row: 1, 2, 2, 3, 99, and NULL remain.
    "EXCEPT ALL subtracts counts": (f"{ORDER_IDS} EXCEPT ALL {CUSTOMER_IDS}", None),
    # Order 106, the only one over 500, has a NULL status.
    "NULLs are equal in set operations": (
        "SELECT status FROM orders INTERSECT "
        "SELECT status FROM orders WHERE amount > 500",
        None,
    ),
    "three queries, left to right": (
        f"{ORDER_IDS} UNION {CUSTOMER_IDS} EXCEPT {ORDER_IDS} WHERE amount > 100",
        None,
    ),
    "parentheses run INTERSECT first": (
        f"{CUSTOMER_IDS} WHERE customer_id > 5 UNION ({ORDER_IDS} INTERSECT "
        f"{CUSTOMER_IDS})",
        None,
    ),
    # int with int, and decimal(10,2) with double: both widen as in Spark SQL.
    "column types widen": (
        "SELECT order_id, amount FROM orders UNION ALL "
        "SELECT customer_id, score FROM customers",
        None,
    ),
    "columns are named after the first query": (
        "SELECT customer_id AS id, name AS label FROM customers UNION "
        "SELECT order_id, status FROM orders",
        None,
    ),
    "aggregates in each query": (
        "SELECT 'orders' AS source, COUNT(*) AS n FROM orders UNION ALL "
        "SELECT 'customers', COUNT(*) FROM customers",
        None,
    ),
    "ORDER BY and LIMIT on the whole result": (
        f"{ORDER_IDS} UNION {CUSTOMER_IDS} ORDER BY customer_id DESC LIMIT 3",
        ["customer_id"],
    ),
    "ORDER BY positions": (
        "SELECT name, country FROM customers UNION ALL "
        "SELECT status, status FROM orders ORDER BY 1 NULLS LAST, 2",
        ["name", "country"],
    ),
    "set operation in a subquery": (
        f"SELECT COUNT(*) AS n FROM ({ORDER_IDS} UNION {CUSTOMER_IDS}) s",
        None,
    ),
    "set operation in a CTE, then joined": (
        f"WITH everyone AS ({ORDER_IDS} UNION {CUSTOMER_IDS}) "
        "SELECT e.customer_id, c.name FROM everyone e "
        "LEFT JOIN customers c ON e.customer_id = c.customer_id",
        None,
    ),
}


@pytest.mark.parametrize(
    ("sql", "order_keys"), SCENARIOS.values(), ids=SCENARIOS.keys()
)
def test_set_operation_scenario(
    spark_tables: "SparkSession", sql: str, order_keys: list[str] | None
) -> None:
    assert_equivalent(spark_tables, sql, order_keys=order_keys)


DIALECT_SCENARIOS = {
    "Oracle MINUS": (
        "oracle",
        f"{CUSTOMER_IDS} MINUS {ORDER_IDS}",
        f"{CUSTOMER_IDS} EXCEPT {ORDER_IDS}",
        None,
    ),
    "Snowflake MINUS": (
        "snowflake",
        f"{CUSTOMER_IDS} MINUS {ORDER_IDS}",
        f"{CUSTOMER_IDS} EXCEPT {ORDER_IDS}",
        None,
    ),
    "BigQuery UNION DISTINCT": (
        "bigquery",
        f"{ORDER_IDS} UNION DISTINCT {CUSTOMER_IDS}",
        f"{ORDER_IDS} UNION {CUSTOMER_IDS}",
        None,
    ),
    "PostgreSQL ORDER BY puts NULLs last": (
        "postgres",
        f"{ORDER_IDS} UNION {CUSTOMER_IDS} ORDER BY customer_id",
        f"{ORDER_IDS} UNION {CUSTOMER_IDS} ORDER BY customer_id NULLS LAST",
        ["customer_id"],
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql", "order_keys"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_set_operation_scenario(
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
