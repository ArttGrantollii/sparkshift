"""Tests for the parts of the harness that need Spark."""

from typing import TYPE_CHECKING

import pytest
from compare import differences, snapshot
from harness import run_generated

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark


def test_generated_code_must_assign_a_dataframe_to_result(
    spark: "SparkSession",
) -> None:
    with pytest.raises(AssertionError, match="must assign a DataFrame to `result`"):
        run_generated(spark, "answer = 42\n")


def test_generated_code_with_a_syntax_error_fails(spark: "SparkSession") -> None:
    with pytest.raises(SyntaxError):
        run_generated(spark, "result = spark.table(\n")


def test_snapshot_records_spark_types(spark: "SparkSession") -> None:
    df = spark.sql(
        "SELECT 1 AS id, CAST(2.5 AS DECIMAL(10, 2)) AS amount, 'x' AS label"
    )

    assert snapshot(df).columns == (
        ("id", "int"),
        ("amount", "decimal(10,2)"),
        ("label", "string"),
    )


def test_nullability_flags_are_ignored(spark: "SparkSession") -> None:
    from pyspark.sql.types import IntegerType, StructField, StructType

    nullable = spark.createDataFrame(
        [(1,)], StructType([StructField("id", IntegerType(), True)])
    )
    required = spark.createDataFrame(
        [(1,)], StructType([StructField("id", IntegerType(), False)])
    )

    assert differences(snapshot(nullable), snapshot(required)) == []
