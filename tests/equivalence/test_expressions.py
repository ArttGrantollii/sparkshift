"""Equivalence scenarios for expressions in the SELECT list.

Each scenario names the semantic risk it guards against.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    "columns": "SELECT customer_id, name FROM customers",
    "qualified columns": "SELECT customers.name, customers.country FROM customers",
    "decimal arithmetic with NULLs": (
        "SELECT order_id, amount * 2 AS doubled, amount + 0.5 AS plus_half, "
        "amount - amount AS zero FROM orders"
    ),
    "decimal literal keeps decimal type": (
        "SELECT order_id, amount * 1.10 AS with_tax, 0.05 AS rate FROM orders"
    ),
    "double arithmetic": (
        "SELECT order_id, discount * 100 AS pct, discount / 2 AS half FROM orders"
    ),
    "integer division and modulo": (
        "SELECT customer_id, customer_id / 2 AS half, customer_id % 3 AS rest "
        "FROM customers"
    ),
    "comparisons with NULL inputs": (
        "SELECT customer_id, score >= 3 AS high, score <> 0 AS nonzero, "
        "country = 'US' AS in_us FROM customers"
    ),
    "three-valued logic": (
        "SELECT customer_id, is_active AND score > 3 AS both_true, "
        "is_active OR score > 3 AS either_true, NOT is_active AS inactive "
        "FROM customers"
    ),
    "precedence": (
        "SELECT order_id, amount + discount * 2 AS a, (amount + 1) * 2 AS b, "
        "NOT status = 'completed' AND amount > 0 AS c, "
        "amount > 100 OR amount < 0 AND status = 'refunded' AS d FROM orders"
    ),
    "empty string is not NULL": (
        "SELECT customer_id, name = '' AS empty_name FROM customers"
    ),
    "every literal type": (
        "SELECT customer_id, 1 AS one, 3000000000 AS big, 1.5 AS exact, "
        "1.5e0 AS approx, 'x' AS text, TRUE AS yes, FALSE AS no, NULL AS nothing, "
        "-5 AS negative FROM customers"
    ),
    "star with expressions": "SELECT *, amount * 2 AS doubled FROM orders",
    "unaliased expressions keep Spark's names": (
        "SELECT order_id, amount * 2, status = 'completed', discount FROM orders"
    ),
    "aliased negation": (
        "SELECT order_id, -amount AS negative, -(amount + 1) AS negative_plus "
        "FROM orders"
    ),
    # Long enough to wrap one operand per line; the grouping must survive.
    "long arithmetic keeps its grouping when wrapped": (
        "SELECT order_id, amount * (1 - discount) + amount * discount / 2 "
        "- (amount - amount * discount) - order_id % 7 "
        "AS adjusted_amount_after_discount FROM orders"
    ),
    "long comparison as a value": (
        "SELECT order_id, amount * (1 - discount) + 10 >= order_id - customer_id * 2 "
        "+ 5 AS above_the_order_threshold FROM orders"
    ),
}


@pytest.mark.parametrize("sql", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_expression_scenario(spark_tables: "SparkSession", sql: str) -> None:
    assert_equivalent(spark_tables, sql)
