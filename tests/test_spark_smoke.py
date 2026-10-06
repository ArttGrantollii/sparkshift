from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


@pytest.mark.spark
def test_local_spark_session_executes_sql(spark: "SparkSession") -> None:
    # Proves the test environment itself works: Java is found, the JVM starts,
    # and Spark can execute a query. Later equivalence tests depend on this.
    rows = spark.sql("SELECT 1 AS x").collect()

    assert [row.asDict() for row in rows] == [{"x": 1}]
