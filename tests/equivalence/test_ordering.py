"""Equivalence scenarios for ORDER BY.

Each scenario names its ``order_keys``: the output columns that decide the
order. Rows equal on all of them may come back in any order, so the harness
compares them as a group. When a query sorts by a column it does not output,
the data has no ties on that column and every output column is a key.

Relevant data: order 105 has a NULL amount, and three orders (102 and the
duplicate rows of 109) tie at 80.00; customer 6 has a NULL name and customer 5
an empty one; product names mix upper and lower case.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    # Spark puts NULLs first when ascending and last when descending.
    "ascending puts NULLs first": (
        "SELECT order_id, amount FROM orders ORDER BY amount",
        ["amount"],
    ),
    "descending puts NULLs last": (
        "SELECT order_id, amount FROM orders ORDER BY amount DESC",
        ["amount"],
    ),
    "explicit NULLS LAST and NULLS FIRST": (
        "SELECT order_id, amount, discount FROM orders "
        "ORDER BY amount NULLS LAST, discount DESC NULLS FIRST",
        ["amount", "discount"],
    ),
    "several keys in mixed directions": (
        "SELECT status, amount, order_id FROM orders "
        "ORDER BY status DESC, amount, order_id",
        ["status", "amount", "order_id"],
    ),
    "positions refer to the SELECT list": (
        "SELECT order_id, amount AS a FROM orders ORDER BY 2 DESC, 1",
        ["a", "order_id"],
    ),
    "alias of an expression": (
        "SELECT order_id, amount * 2 AS double_amount FROM orders "
        "ORDER BY double_amount",
        ["double_amount"],
    ),
    # Standard SQL: a plain ORDER BY name means the output column. Sorting by
    # the input order_id instead would give a different order.
    "alias shadowing a column wins": (
        "SELECT customer_id AS order_id, order_id AS customer_id FROM orders "
        "ORDER BY order_id DESC",
        ["order_id"],
    ),
    # customer_id ties only between the two identical rows of customer 7.
    "column that is not selected": (
        "SELECT name FROM customers ORDER BY customer_id DESC",
        ["name"],
    ),
    "expression that is not selected": (
        "SELECT order_id FROM orders ORDER BY amount * -1, order_id",
        ["order_id"],
    ),
    "expression on a selected column": (
        "SELECT name FROM customers ORDER BY LENGTH(name) DESC, name",
        ["name"],
    ),
    # Spark compares strings by code point: '' first, upper case before
    # lower case. (Some databases sort case-insensitively; see the README.)
    "strings sort case-sensitively": (
        "SELECT product_id, name FROM products ORDER BY name",
        ["name"],
    ),
    "booleans and dates": (
        "SELECT customer_id, signup_date FROM customers "
        "ORDER BY is_active DESC, signup_date",
        ["customer_id", "signup_date"],
    ),
    "doubles with a NULL": (
        "SELECT customer_id, score FROM customers ORDER BY score",
        ["score"],
    ),
    # Completed amounts sorted descending: 120.50, then three tied at 80.00.
    # The LIMIT keeps two of the three; either two are correct.
    "top-N cutting a group of ties": (
        "SELECT order_id, amount FROM orders WHERE status = 'completed' "
        "ORDER BY amount DESC LIMIT 3",
        ["amount"],
    ),
    "DISTINCT then ORDER BY": (
        "SELECT DISTINCT status FROM orders ORDER BY status",
        ["status"],
    ),
    "aggregate alias": (
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status "
        "ORDER BY n DESC, status",
        ["n", "status"],
    ),
    # Each status has a different total; the pending total is NULL.
    "aggregate that is not selected": (
        "SELECT status FROM orders GROUP BY status ORDER BY SUM(amount) DESC",
        ["status"],
    ),
    "group key that is not selected": (
        "SELECT COUNT(*) AS n, MAX(order_id) AS last_order FROM orders "
        "GROUP BY status ORDER BY status",
        ["n", "last_order"],
    ),
    "qualified columns of a join": (
        "SELECT c.name, o.amount FROM customers c "
        "JOIN orders o ON c.customer_id = o.customer_id "
        "ORDER BY o.amount DESC, c.name",
        ["amount", "name"],
    ),
    "SELECT * with WHERE": (
        "SELECT * FROM products WHERE stock > 0 ORDER BY price DESC",
        ["price"],
    ),
}


@pytest.mark.parametrize(
    ("sql", "order_keys"), SCENARIOS.values(), ids=SCENARIOS.keys()
)
def test_ordering_scenario(
    spark_tables: "SparkSession", sql: str, order_keys: list[str]
) -> None:
    assert_equivalent(spark_tables, sql, order_keys=order_keys)


# Each dialect's default NULL placement, spelled out in the Spark reference.
DIALECT_SCENARIOS = {
    "PostgreSQL ascending puts NULLs last": (
        "postgres",
        "SELECT order_id, amount FROM orders ORDER BY amount",
        "SELECT order_id, amount FROM orders ORDER BY amount NULLS LAST",
        ["amount"],
    ),
    "Oracle descending puts NULLs first": (
        "oracle",
        "SELECT order_id, amount FROM orders ORDER BY amount DESC",
        "SELECT order_id, amount FROM orders ORDER BY amount DESC NULLS FIRST",
        ["amount"],
    ),
    "Snowflake after GROUP BY": (
        "snowflake",
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY status",
        "SELECT status, COUNT(*) AS n FROM orders GROUP BY status "
        "ORDER BY status NULLS LAST",
        ["status"],
    ),
    "MySQL ascending puts NULLs first, like Spark": (
        "mysql",
        "SELECT customer_id, name FROM customers ORDER BY name",
        "SELECT customer_id, name FROM customers ORDER BY name NULLS FIRST",
        ["name"],
    ),
    # Spark SQL 4.2 rejects an aggregate shared by HAVING and ORDER BY
    # (UNSUPPORTED_EXPR_FOR_OPERATOR), so the reference uses a subquery. The
    # generated code computes the aggregate once, as a helper column.
    "PostgreSQL aggregate shared by HAVING and ORDER BY": (
        "postgres",
        "SELECT status FROM orders GROUP BY status HAVING COUNT(*) >= 1 "
        "ORDER BY COUNT(*) DESC, status",
        "SELECT status FROM (SELECT status, COUNT(*) AS n FROM orders "
        "GROUP BY status HAVING COUNT(*) >= 1) ORDER BY n DESC, status NULLS LAST",
        ["status"],
    ),
    "T-SQL TOP with ORDER BY": (
        "tsql",
        "SELECT TOP 3 order_id, amount FROM orders ORDER BY amount DESC",
        "SELECT order_id, amount FROM orders ORDER BY amount DESC LIMIT 3",
        ["amount"],
    ),
    "Oracle FETCH FIRST with explicit NULLS FIRST": (
        "oracle",
        "SELECT order_id, amount FROM orders "
        "ORDER BY amount NULLS FIRST FETCH FIRST 2 ROWS ONLY",
        "SELECT order_id, amount FROM orders ORDER BY amount NULLS FIRST LIMIT 2",
        ["amount"],
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql", "order_keys"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_ordering_scenario(
    spark_tables: "SparkSession",
    dialect: str,
    sql: str,
    reference_sql: str,
    order_keys: list[str],
) -> None:
    assert_equivalent(
        spark_tables,
        sql,
        dialect=dialect,
        reference_sql=reference_sql,
        order_keys=order_keys,
    )
