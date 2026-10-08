from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from datasets import TABLES

if TYPE_CHECKING:
    from pyspark.sql import SparkSession


@pytest.fixture(scope="session")
def spark_tables(spark: "SparkSession") -> Iterator["SparkSession"]:
    """The shared SparkSession with every test table registered as a temp view.

    Temp views live in memory only; nothing is written to disk.
    """
    for name, (schema, rows) in TABLES.items():
        spark.createDataFrame(rows, schema).createOrReplaceTempView(name)
    yield spark
    for name in TABLES:
        spark.catalog.dropTempView(name)
