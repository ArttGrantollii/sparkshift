"""Equivalence scenarios for string and numeric functions.

The products table holds the edge cases: names with leading and trailing
spaces, trailing spaces only, multi-byte characters, an empty string, and
NULL; prices and weights at rounding halves, including negative ones.
"""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark

SCENARIOS = {
    "case conversion of non-ASCII text": (
        "SELECT product_id, UPPER(name) AS upper_name, LOWER(name) AS lower_name "
        "FROM products"
    ),
    "length and trimming": (
        "SELECT product_id, LENGTH(name) AS len, TRIM(name) AS t, LTRIM(name) AS l, "
        "RTRIM(name) AS r, LENGTH(TRIM(name)) AS trimmed_len FROM products"
    ),
    "substring with and without a length": (
        "SELECT product_id, SUBSTRING(name, 2, 3) AS middle, SUBSTR(name, 3) AS rest, "
        "SUBSTRING(name, 20, 5) AS beyond_end FROM products"
    ),
    # Spark's CONCAT and || return NULL when any input is NULL.
    "concat propagates NULL": (
        "SELECT product_id, CONCAT(name, '-', category) AS label, "
        "category || '/' || name AS path FROM products"
    ),
    "replace, left, right": (
        "SELECT product_id, REPLACE(name, 'e', 'E') AS replaced, "
        "LEFT(name, 3) AS head, "
        "RIGHT(name, 3) AS tail, LEFT(name, 0) AS nothing FROM products"
    ),
    "round decimals and doubles at halves": (
        "SELECT product_id, ROUND(price) AS p0, ROUND(price, 2) AS p2, "
        "ROUND(price, -1) AS p_tens, ROUND(weight) AS w0, ROUND(weight, 1) AS w1 "
        "FROM products"
    ),
    # CEIL and FLOOR of a decimal stay decimal; of a double they become bigint.
    "ceil and floor on negatives": (
        "SELECT product_id, CEIL(price) AS c, CEILING(weight) AS cw, "
        "FLOOR(price) AS f, "
        "FLOOR(weight) AS fw FROM products"
    ),
    # SQRT of a negative number is NaN; LN of a non-positive number is NULL.
    "power, square root, logarithms": (
        "SELECT product_id, POWER(stock, 2) AS squared, SQRT(stock) AS root, "
        "LN(stock) AS natural, LOG(10, stock) AS base10, EXP(stock) AS e FROM products"
    ),
    "abs and sign": (
        "SELECT product_id, ABS(price) AS a, ABS(stock) AS ai, SIGN(weight) AS s "
        "FROM products"
    ),
    # Spark's GREATEST and LEAST skip NULL arguments.
    "greatest and least skip NULLs": (
        "SELECT product_id, GREATEST(price, weight, stock) AS high, "
        "LEAST(price, weight, stock) AS low FROM products"
    ),
    "functions in WHERE": (
        "SELECT product_id FROM products WHERE UPPER(category) = 'TOOLS' "
        "AND LENGTH(TRIM(name)) > 0"
    ),
    "functions inside aggregates and CASE": (
        "SELECT category, COUNT(*) AS n, MAX(LENGTH(name)) AS longest, "
        "SUM(CASE WHEN ROUND(price) > 0 THEN 1 ELSE 0 END) AS positive "
        "FROM products GROUP BY category"
    ),
}


@pytest.mark.parametrize("sql", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_function_scenario(spark_tables: "SparkSession", sql: str) -> None:
    assert_equivalent(spark_tables, sql)


# Each emulation of a dialect's semantics, checked against hand-written Spark SQL.
DIALECT_SCENARIOS = {
    # T-SQL LEN ignores trailing spaces; T-SQL CONCAT skips NULLs.
    "T-SQL LEN and CONCAT": (
        "tsql",
        "SELECT product_id, LEN(name) AS n, LEN(stock) AS digits, "
        "CONCAT(name, category) AS c FROM products",
        "SELECT product_id, LENGTH(RTRIM(name)) AS n, "
        "LENGTH(RTRIM(CAST(stock AS STRING))) AS digits, "
        "CONCAT_WS('', name, category) AS c FROM products",
    ),
    # PostgreSQL CONCAT skips NULLs, but || propagates them.
    "PostgreSQL CONCAT and ||": (
        "postgres",
        "SELECT product_id, CONCAT(name, '-', category) AS c, "
        "name || category AS p FROM products",
        "SELECT product_id, CONCAT_WS('', name, '-', category) AS c, "
        "CONCAT(name, category) AS p FROM products",
    ),
    # MySQL LENGTH counts bytes; CHAR_LENGTH counts characters.
    "MySQL LENGTH and CHAR_LENGTH": (
        "mysql",
        "SELECT product_id, LENGTH(name) AS bytes, CHAR_LENGTH(name) AS chars "
        "FROM products",
        "SELECT product_id, OCTET_LENGTH(name) AS bytes, LENGTH(name) AS chars "
        "FROM products",
    ),
    # Oracle || treats NULL as an empty string.
    "Oracle ||": (
        "oracle",
        "SELECT product_id, name || category AS c FROM products",
        "SELECT product_id, CONCAT_WS('', name, category) AS c FROM products",
    ),
    # Snowflake rounds halves away from zero for every numeric type, like Spark.
    "Snowflake ROUND": (
        "snowflake",
        "SELECT product_id, ROUND(price, 1) AS r FROM products",
        "SELECT product_id, ROUND(price, 1) AS r FROM products",
    ),
}


@pytest.mark.parametrize(
    ("dialect", "sql", "reference_sql"),
    DIALECT_SCENARIOS.values(),
    ids=DIALECT_SCENARIOS.keys(),
)
def test_dialect_function_scenario(
    spark_tables: "SparkSession", dialect: str, sql: str, reference_sql: str
) -> None:
    assert_equivalent(spark_tables, sql, dialect=dialect, reference_sql=reference_sql)
