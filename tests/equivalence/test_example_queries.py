"""Every example in examples/sql returns the same result as its SQL on Spark.

Examples in a dialect Spark cannot run have a hand-written Spark SQL
reference here.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

EXAMPLES = Path(__file__).parents[2] / "examples" / "sql"

# example -> (Spark SQL reference, or None to run the example itself;
#             ORDER BY keys, or None if the query has no ORDER BY)
REFERENCES: dict[str, tuple[str | None, list[str] | None]] = {
    "generic/revenue_ranking": (None, ["revenue_rank", "name"]),
    "postgres/latest_order": (
        "WITH numbered AS (SELECT customer_id, order_id, order_date, amount, "
        "ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_date DESC "
        "NULLS FIRST, order_id DESC NULLS FIRST) AS recency FROM orders) "
        "SELECT customer_id, order_id, order_date, amount FROM numbered "
        "WHERE recency = 1 ORDER BY amount NULLS LAST",
        ["amount"],
    ),
    "tsql/top_customers": (
        "SELECT c.name, LENGTH(RTRIM(c.name)) AS name_length, "
        "SUM(o.amount) AS total_spent FROM customers c "
        "JOIN orders o ON o.customer_id = c.customer_id GROUP BY c.name "
        "ORDER BY total_spent DESC LIMIT 3",
        ["total_spent"],
    ),
    "mysql/name_lengths": (
        "SELECT product_id, name, OCTET_LENGTH(name) AS bytes, "
        "CHAR_LENGTH(name) AS characters, "
        "COALESCE(category, 'uncategorized') AS category "
        "FROM products WHERE name IS NOT NULL",
        None,
    ),
    "snowflake/customers_without_orders": (
        "SELECT c.customer_id, c.name, c.country FROM customers c "
        "WHERE NOT EXISTS (SELECT 1 FROM orders o "
        "WHERE o.customer_id = c.customer_id) ORDER BY c.name NULLS LAST",
        ["name"],
    ),
    "bigquery/monthly_revenue": (
        "SELECT month, SUM(amount) AS revenue, "
        "COUNT(DISTINCT customer_id) AS customers FROM (SELECT "
        "TRUNC(order_date, 'MM') AS month, amount, customer_id FROM orders) "
        "GROUP BY month ORDER BY month",
        ["month"],
    ),
    "oracle/never_cancelled": (
        "SELECT customer_id FROM customers EXCEPT "
        "SELECT COALESCE(customer_id, -1) AS customer_id FROM orders "
        "WHERE status = 'cancelled' ORDER BY customer_id NULLS LAST",
        ["customer_id"],
    ),
}


def test_every_example_has_a_reference() -> None:
    examples = {f"{path.parent.name}/{path.stem}" for path in EXAMPLES.rglob("*.sql")}

    assert examples == set(REFERENCES)


@pytest.mark.spark
@pytest.mark.parametrize("name", sorted(REFERENCES))
def test_example_scenario(spark_tables: "SparkSession", name: str) -> None:
    folder = name.split("/")[0]
    reference_sql, order_keys = REFERENCES[name]

    assert_equivalent(
        spark_tables,
        (EXAMPLES / f"{name}.sql").read_text(encoding="utf-8"),
        dialect=None if folder == "generic" else folder,
        reference_sql=reference_sql,
        order_keys=order_keys,
    )
