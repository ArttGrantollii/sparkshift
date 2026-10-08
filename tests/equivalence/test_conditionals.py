"""Equivalence scenarios for predicates, CASE, NULL functions, and CAST.

Each scenario names the SQL rule it proves.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    # x NOT IN (..., NULL) is never true: every row is filtered out.
    "NOT IN with a NULL returns no rows": (
        "SELECT order_id FROM orders WHERE customer_id NOT IN (1, NULL)"
    ),
    "NOT IN without NULLs": (
        "SELECT order_id FROM orders WHERE customer_id NOT IN (1, 2)"
    ),
    # x IN (1, NULL) is TRUE on a match and NULL otherwise, never FALSE.
    "IN with a NULL in the list": (
        "SELECT order_id, customer_id IN (1, NULL) AS matched FROM orders"
    ),
    "IN on strings": ("SELECT * FROM customers WHERE country IN ('US', 'ES')"),
    "BETWEEN is inclusive": (
        "SELECT order_id FROM orders WHERE amount BETWEEN 80 AND 120.50"
    ),
    "BETWEEN with reversed bounds matches nothing": (
        "SELECT order_id FROM orders WHERE amount BETWEEN 500 AND 1"
    ),
    "NOT BETWEEN and dates": (
        "SELECT order_id, order_date NOT BETWEEN DATE '2024-01-10' "
        "AND DATE '2024-02-29' AS outside FROM orders"
    ),
    "LIKE wildcards and NULL input": (
        "SELECT customer_id, name LIKE 'A%' AS a_names, name LIKE '_o%' AS o_second, "
        "name NOT LIKE '%e' AS no_e FROM customers"
    ),
    "ILIKE ignores case": (
        "SELECT customer_id FROM customers WHERE name ILIKE 'grace'"
    ),
    "IS NULL across types": (
        "SELECT customer_id, name IS NULL AS no_name, "
        "country IS NOT NULL AS has_country, "
        "signup_date IS NULL AS no_date, is_active IS NULL AS unknown_active "
        "FROM customers"
    ),
    # NULL-safe equality matches NULL with NULL, unlike "=".
    "null-safe equality": (
        "SELECT a.customer_id, b.customer_id AS other "
        "FROM customers a JOIN customers b "
        "ON a.country IS NOT DISTINCT FROM b.country AND a.customer_id < b.customer_id"
    ),
    "IS DISTINCT FROM": (
        "SELECT order_id, status IS DISTINCT FROM 'completed' AS not_completed "
        "FROM orders"
    ),
    # First match wins; no ELSE gives NULL.
    "searched CASE": (
        "SELECT order_id, CASE WHEN amount > 100 THEN 'big' WHEN amount > 50 "
        "THEN 'medium' WHEN amount > 0 THEN 'small' END AS size FROM orders"
    ),
    # A NULL subject never matches any WHEN, so the ELSE branch applies.
    "simple CASE with a NULL subject": (
        "SELECT customer_id, CASE country WHEN 'US' THEN 'domestic' "
        "WHEN 'ES' THEN 'spain' ELSE 'other' END AS region FROM customers"
    ),
    "conditional counting with SUM(CASE ...)": (
        "SELECT customer_id, SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) "
        "AS completed, COUNT(*) AS total FROM orders GROUP BY customer_id"
    ),
    # COALESCE picks a common type: decimal and int give decimal; decimal and
    # double give double.
    "COALESCE type widening": (
        "SELECT order_id, COALESCE(amount, 0) AS amount_or_zero, "
        "COALESCE(status, 'unknown') AS s, COALESCE(discount, amount) AS mixed "
        "FROM orders"
    ),
    "NULLIF": (
        "SELECT order_id, NULLIF(amount, 0) AS nonzero, NULLIF(status, 'pending') "
        "AS not_pending FROM orders"
    ),
    "CAST rounding and conversions": (
        "SELECT order_id, CAST(amount AS INT) AS whole, "
        "CAST(amount AS DECIMAL(10,1)) AS one_place, "
        "CAST(1.005 AS DECIMAL(10,2)) AS rounded, CAST(order_id AS STRING) AS id_text, "
        "CAST('2024-03-01' AS DATE) AS a_date, CAST(discount AS DOUBLE) AS d "
        "FROM orders"
    ),
    "TRY_CAST returns NULL for bad input": (
        "SELECT customer_id, TRY_CAST(name AS INT) AS name_number FROM customers"
    ),
}


@pytest.mark.parametrize("sql", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_conditional_scenario(spark_tables: "SparkSession", sql: str) -> None:
    assert_equivalent(spark_tables, sql)


DIALECT_SCENARIOS = {
    "T-SQL IIF": (
        "tsql",
        "SELECT order_id, IIF(amount > 100, 'big', 'small') AS size FROM orders",
        "SELECT order_id, IF(amount > 100, 'big', 'small') AS size FROM orders",
    ),
    "PostgreSQL :: cast": (
        "postgres",
        "SELECT order_id, amount::numeric(10,1) AS rounded FROM orders",
        "SELECT order_id, CAST(amount AS DECIMAL(10,1)) AS rounded FROM orders",
    ),
    # Spark SQL rejects VARCHAR without a length; PostgreSQL means unbounded.
    "PostgreSQL VARCHAR without a length": (
        "postgres",
        "SELECT order_id, CAST(order_id AS VARCHAR) AS id_text FROM orders",
        "SELECT order_id, CAST(order_id AS STRING) AS id_text FROM orders",
    ),
    "MySQL IFNULL and <=>": (
        "mysql",
        "SELECT order_id, IFNULL(status, 'none') AS s, status <=> NULL AS no_status "
        "FROM orders",
        "SELECT order_id, COALESCE(status, 'none') AS s, status <=> NULL AS no_status "
        "FROM orders",
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_conditional_scenario(
    spark_tables: "SparkSession", dialect: str, sql: str, reference_sql: str
) -> None:
    assert_equivalent(spark_tables, sql, dialect=dialect, reference_sql=reference_sql)
