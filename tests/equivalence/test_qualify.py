"""Equivalence scenarios for QUALIFY, which filters rows on window functions.

QUALIFY runs after the window functions are computed and before DISTINCT,
ORDER BY, and LIMIT. Spark SQL runs QUALIFY itself, so these scenarios use
the original SQL as the reference.

As in the window scenarios, where ROW_NUMBER picks one row of a partition,
the window's order has no ties except identical rows (the duplicates of
order 109), which give the same result whichever is picked.

Each scenario is (sql, order_keys): order_keys is None unless the query itself
has ORDER BY.

Relevant data: order 105 has a NULL amount; order 103 a NULL discount; order
107 a NULL customer; customers 1 and 2 have two completed orders each
(customer 2's are identical rows).
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    # The NULL customer forms its own partition.
    "latest order per customer": (
        "SELECT customer_id, order_id, order_date FROM orders "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id "
        "ORDER BY order_date DESC, order_id DESC) = 1",
        None,
    ),
    "SELECT * keeps every column and drops the helper": (
        "SELECT * FROM orders "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY status ORDER BY created_at, order_id) "
        "= 1",
        None,
    ),
    # Customer 2's two 80.00 orders tie for first place; RANK keeps both.
    "RANK keeps ties": (
        "SELECT customer_id, order_id, amount FROM orders "
        "QUALIFY RANK() OVER (PARTITION BY customer_id ORDER BY amount DESC) = 1",
        None,
    ),
    "reference to a window's alias": (
        "SELECT customer_id, order_id, "
        "DENSE_RANK() OVER (PARTITION BY customer_id ORDER BY amount) AS amount_rank "
        "FROM orders QUALIFY amount_rank = 1",
        None,
    ),
    "inline window that is also selected": (
        "SELECT customer_id, order_id, "
        "ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id) AS position "
        "FROM orders "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id) = 1",
        None,
    ),
    # The totals include the orders QUALIFY then removes: windows come first.
    "windows are computed before QUALIFY filters": (
        "SELECT order_id, customer_id, "
        "SUM(amount) OVER (PARTITION BY customer_id) AS customer_total "
        "FROM orders QUALIFY status = 'completed'",
        None,
    ),
    # Only customers 1 and 2 have two rows with the same status; after
    # DISTINCT, every count would be 1 and nothing would be left.
    "QUALIFY runs before DISTINCT": (
        "SELECT DISTINCT customer_id, status FROM orders "
        "QUALIFY COUNT(*) OVER (PARTITION BY customer_id, status) > 1",
        None,
    ),
    # Customer 1's two orders are numbered 1 and 2; DISTINCT must see them
    # after the numbers are filtered and dropped, or both would remain.
    "DISTINCT removes the rows QUALIFY keeps": (
        "SELECT DISTINCT customer_id FROM orders "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id) <= 2",
        None,
    ),
    # WHERE runs before the window, ORDER BY and LIMIT after QUALIFY.
    "QUALIFY with WHERE, ORDER BY, and LIMIT": (
        "SELECT customer_id, order_id, amount FROM orders WHERE status = 'completed' "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id "
        "ORDER BY amount DESC, order_id) = 1 ORDER BY amount DESC LIMIT 3",
        ["amount"],
    ),
    # A NULL condition drops the row, as in WHERE.
    "NULL condition drops the row": (
        "SELECT order_id, discount FROM orders "
        "QUALIFY discount > AVG(discount) OVER ()",
        None,
    ),
    "QUALIFY in a CTE": (
        "WITH latest AS (SELECT customer_id, amount FROM orders "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id "
        "ORDER BY order_date DESC, order_id DESC) = 1) "
        "SELECT c.name, l.amount FROM customers c "
        "JOIN latest l ON l.customer_id = c.customer_id",
        None,
    ),
}


@pytest.mark.parametrize(
    ("sql", "order_keys"), SCENARIOS.values(), ids=SCENARIOS.keys()
)
def test_qualify_scenario(
    spark_tables: "SparkSession", sql: str, order_keys: list[str] | None
) -> None:
    assert_equivalent(spark_tables, sql, order_keys=order_keys)


DIALECT_SCENARIOS = {
    # Snowflake sorts NULLs first when descending, so customer 3's latest
    # "largest" order is the one with a NULL amount; Spark would pick order 104.
    "Snowflake picks the NULL amount first when descending": (
        "snowflake",
        "SELECT customer_id, order_id FROM orders "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY amount DESC, "
        "order_id) = 1",
        "SELECT customer_id, order_id FROM orders "
        "QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id "
        "ORDER BY amount DESC NULLS FIRST, order_id) = 1",
        None,
    ),
    "BigQuery reference to a window's alias": (
        "bigquery",
        "SELECT customer_id, order_id, "
        "RANK() OVER (PARTITION BY customer_id ORDER BY amount) AS amount_rank "
        "FROM orders QUALIFY amount_rank = 1 ORDER BY order_id",
        "SELECT customer_id, order_id, "
        "RANK() OVER (PARTITION BY customer_id ORDER BY amount) AS amount_rank "
        "FROM orders QUALIFY amount_rank = 1 ORDER BY order_id",
        ["order_id"],
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql", "order_keys"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_qualify_scenario(
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
