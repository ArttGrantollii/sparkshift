"""Equivalence scenarios for joins.

The datasets are built for join semantics: order 107 has a NULL customer_id,
order 108 belongs to customer 99 who does not exist, customer 7 (Grace) has no
orders and appears twice, and several customers have more than one order.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    # NULL keys never match: order 107 is absent, and so is Grace.
    "inner join with aliases": (
        "SELECT c.country, o.order_id, o.amount FROM customers c "
        "JOIN orders o ON c.customer_id = o.customer_id"
    ),
    # Both customer_id columns appear, left table's columns first.
    "select star keeps both same-named columns": (
        "SELECT * FROM customers c INNER JOIN orders o ON c.customer_id = o.customer_id"
    ),
    # Grace (no orders) appears once per duplicate row, with NULL order columns.
    "left join keeps unmatched rows": (
        "SELECT c.customer_id, c.name, o.order_id FROM customers c "
        "LEFT JOIN orders o ON c.customer_id = o.customer_id"
    ),
    # Orders 107 (NULL customer) and 108 (unknown customer) appear with NULLs.
    "right join keeps unmatched rows": (
        "SELECT c.name, o.order_id, o.customer_id FROM customers c "
        "RIGHT OUTER JOIN orders o ON c.customer_id = o.customer_id"
    ),
    "full join keeps both sides": (
        "SELECT c.customer_id AS c_id, o.customer_id AS o_id, o.order_id "
        "FROM customers c FULL OUTER JOIN orders o ON c.customer_id = o.customer_id"
    ),
    "cross join": "SELECT c.name, o.order_id FROM customers c CROSS JOIN orders o",
    "comma join with WHERE": (
        "SELECT c.name, o.amount FROM customers c, orders o "
        "WHERE c.customer_id = o.customer_id AND o.amount > 0"
    ),
    "multiple conditions including a non-equality": (
        "SELECT c.customer_id, o.order_id FROM customers c JOIN orders o "
        "ON c.customer_id = o.customer_id AND o.order_date > c.signup_date "
        "AND o.amount >= 50"
    ),
    "tables as qualifiers without aliases": (
        "SELECT customers.name, orders.amount FROM customers "
        "JOIN orders ON customers.customer_id = orders.customer_id"
    ),
    # Each pair of a customer's orders, earlier order first.
    "self join": (
        "SELECT o1.order_id AS first_id, o2.order_id AS second_id FROM orders o1 "
        "JOIN orders o2 ON o1.customer_id = o2.customer_id "
        "AND o1.order_id < o2.order_id"
    ),
    "three-way join": (
        "SELECT c.name, o1.order_id, o2.order_id AS other_id FROM customers c "
        "JOIN orders o1 ON c.customer_id = o1.customer_id "
        "LEFT JOIN orders o2 ON o1.customer_id = o2.customer_id "
        "AND o2.order_id > o1.order_id"
    ),
    # USING produces one customer_id column instead of two.
    "inner join USING": "SELECT * FROM customers JOIN orders USING (customer_id)",
    # In a FULL JOIN, the USING column takes whichever side is not NULL.
    "full join USING": (
        "SELECT customer_id, name, order_id FROM customers "
        "FULL JOIN orders USING (customer_id)"
    ),
    "qualified star": (
        "SELECT c.*, o.amount FROM customers c JOIN orders o "
        "ON c.customer_id = o.customer_id"
    ),
    "join, filter, distinct, limit": (
        "SELECT DISTINCT c.country FROM customers c JOIN orders o "
        "ON c.customer_id = o.customer_id WHERE o.status = 'completed' LIMIT 2"
    ),
}


@pytest.mark.parametrize("sql", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_join_scenario(spark_tables: "SparkSession", sql: str) -> None:
    assert_equivalent(spark_tables, sql)
