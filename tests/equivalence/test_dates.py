"""Equivalence scenarios for dates, timestamps, and time zones.

Relevant data: orders.order_date is a DATE and orders.created_at a TIMESTAMP,
including a leap-day timestamp with microseconds (2024-02-29 23:59:59.999999)
and timestamps late in the day. The test session's time zone is UTC.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    "date parts from dates": (
        "SELECT order_id, EXTRACT(YEAR FROM order_date) AS y, "
        "EXTRACT(QUARTER FROM order_date) AS q, EXTRACT(MONTH FROM order_date) AS m, "
        "EXTRACT(DAY FROM order_date) AS d FROM orders"
    ),
    "date parts from timestamps": (
        "SELECT order_id, EXTRACT(DAY FROM created_at) AS d, "
        "EXTRACT(HOUR FROM created_at) AS h, EXTRACT(MINUTE FROM created_at) AS mi, "
        "YEAR(created_at) AS y FROM orders"
    ),
    "date parts in WHERE": (
        "SELECT order_id FROM orders WHERE EXTRACT(MONTH FROM order_date) = 1"
    ),
    # Adding to a date gives a date; adding to a timestamp keeps the time.
    "day arithmetic keeps the type": (
        "SELECT order_id, order_date + INTERVAL '3' DAY AS d3, "
        "created_at + INTERVAL '3' DAY AS t3, order_date - INTERVAL '1' DAY AS d_prev, "
        "order_date + 7 AS d_week FROM orders"
    ),
    # Jan 31 + 1 month is Feb 29 (2024 is a leap year); Feb 29 + 1 year is Feb 28.
    "month and year arithmetic at month ends": (
        "SELECT order_id, DATE '2024-01-31' + INTERVAL '1' MONTH AS feb, "
        "DATE '2024-02-29' + INTERVAL '1' YEAR AS next_year, "
        "order_date + INTERVAL '2' WEEK AS two_weeks, "
        "created_at - INTERVAL '1' MONTH AS month_before FROM orders"
    ),
    # Spark's own DATE_ADD(x, n) always returns a date, even for a timestamp.
    "Spark DATE_ADD and DATE_SUB with a day count": (
        "SELECT order_id, DATE_ADD(order_date, 3) AS later, "
        "DATE_SUB(created_at, 1) AS earlier_day FROM orders"
    ),
    # Day differences count calendar-day boundaries, also for timestamps.
    "day differences": (
        "SELECT order_id, DATEDIFF(order_date, DATE '2024-01-01') AS since_new_year, "
        "DATEDIFF(created_at, order_date) AS same_day FROM orders"
    ),
    "day difference across a year boundary": (
        "SELECT c.customer_id, o.order_id, "
        "DATEDIFF(o.order_date, c.signup_date) AS waited "
        "FROM customers c JOIN orders o ON c.customer_id = o.customer_id"
    ),
    "truncation to year, quarter, month": (
        "SELECT order_id, DATE_TRUNC('YEAR', order_date) AS y, "
        "DATE_TRUNC('QUARTER', created_at) AS q, DATE_TRUNC('MONTH', created_at) AS m "
        "FROM orders"
    ),
    "current date and time through stable predicates": (
        "SELECT order_id, CURRENT_DATE >= order_date AS in_past, "
        "CURRENT_TIMESTAMP > created_at AS before_now FROM orders"
    ),
    "timestamp casts": (
        "SELECT order_id, CAST(order_date AS TIMESTAMP) AS midnight, "
        "CAST('2024-01-01 10:30:00' AS TIMESTAMP) AS fixed, "
        "CAST(created_at AS TIMESTAMP_NTZ) AS wall_clock, "
        "CAST(created_at AS DATE) AS created_day FROM orders"
    ),
}


@pytest.mark.parametrize("sql", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_date_scenario(spark_tables: "SparkSession", sql: str) -> None:
    assert_equivalent(spark_tables, sql)


# Each dialect mapping, checked against hand-written Spark SQL.
DIALECT_SCENARIOS = {
    # The comparison is too long for one line, so it wraps before ">=".
    "T-SQL DATEADD in a long WHERE comparison": (
        "tsql",
        "SELECT o.order_id FROM orders o "
        "WHERE o.order_date >= DATEADD(day, -30, CAST('2024-03-01' AS DATE))",
        "SELECT o.order_id FROM orders o "
        "WHERE o.order_date >= DATE '2024-03-01' - INTERVAL '30' DAY",
    ),
    # DATEDIFF(day, start, end): the argument order is the reverse of Spark's.
    "T-SQL DATEDIFF, DATEADD, DATEPART, DATETIME2": (
        "tsql",
        "SELECT order_id, DATEDIFF(day, order_date, created_at) AS d, "
        "DATEDIFF(day, '2024-01-01', order_date) AS since, "
        "DATEADD(day, 3, created_at) AS later, DATEPART(month, order_date) AS m, "
        "YEAR(order_date) AS y, CAST(created_at AS DATETIME2) AS t FROM orders",
        "SELECT order_id, DATEDIFF(created_at, order_date) AS d, "
        "DATEDIFF(order_date, '2024-01-01') AS since, "
        "created_at + INTERVAL '3' DAY AS later, MONTH(order_date) AS m, "
        "YEAR(order_date) AS y, CAST(created_at AS TIMESTAMP_NTZ) AS t FROM orders",
    ),
    "Snowflake DATEDIFF and DATEADD by month": (
        "snowflake",
        "SELECT order_id, DATEDIFF(day, order_date, DATE '2024-03-31') AS left_in_q1, "
        "DATEADD(month, 1, order_date) AS next_month FROM orders",
        "SELECT order_id, DATEDIFF(DATE '2024-03-31', order_date) AS left_in_q1, "
        "order_date + INTERVAL '1' MONTH AS next_month FROM orders",
    ),
    # MySQL DATEDIFF(end, start) has the same order as Spark's.
    "MySQL DATEDIFF and DATE_ADD": (
        "mysql",
        "SELECT order_id, DATEDIFF(order_date, '2024-01-01') AS d, "
        "DATE_ADD(order_date, INTERVAL 3 DAY) AS later, "
        "DATE_SUB(created_at, INTERVAL 1 DAY) AS earlier FROM orders",
        "SELECT order_id, DATEDIFF(order_date, '2024-01-01') AS d, "
        "order_date + INTERVAL '3' DAY AS later, "
        "created_at - INTERVAL '1' DAY AS earlier FROM orders",
    ),
    # BigQuery's DATE_TRUNC returns a date.
    "BigQuery DATE_DIFF and DATE_TRUNC": (
        "bigquery",
        "SELECT order_id, DATE_DIFF(order_date, DATE '2024-01-01', DAY) AS d, "
        "DATE_TRUNC(order_date, MONTH) AS m FROM orders",
        "SELECT order_id, DATEDIFF(order_date, DATE '2024-01-01') AS d, "
        "TRUNC(order_date, 'MONTH') AS m FROM orders",
    ),
    # PostgreSQL's DATE_TRUNC returns a timestamp; its TIMESTAMP is wall-clock.
    "PostgreSQL DATE_TRUNC, interval, TIMESTAMP": (
        "postgres",
        "SELECT order_id, DATE_TRUNC('month', order_date) AS m, "
        "order_date + INTERVAL '3 days' AS later, "
        "CAST(created_at AS TIMESTAMP) AS wall, "
        "CAST(created_at AS TIMESTAMPTZ) AS instant FROM orders",
        "SELECT order_id, DATE_TRUNC('MONTH', order_date) AS m, "
        "order_date + INTERVAL '3' DAY AS later, "
        "CAST(created_at AS TIMESTAMP_NTZ) AS wall, "
        "CAST(created_at AS TIMESTAMP) AS instant FROM orders",
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_date_scenario(
    spark_tables: "SparkSession", dialect: str, sql: str, reference_sql: str
) -> None:
    assert_equivalent(spark_tables, sql, dialect=dialect, reference_sql=reference_sql)
