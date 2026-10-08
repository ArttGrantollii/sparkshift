"""Equivalence scenarios: the original SQL and the generated PySpark must return
the same result on real Spark."""

from typing import TYPE_CHECKING

import pytest
from harness import assert_equivalent

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM customers",
        "SELECT * FROM orders",
    ],
)
def test_select_star(spark_tables: "SparkSession", sql: str) -> None:
    assert_equivalent(spark_tables, sql)
