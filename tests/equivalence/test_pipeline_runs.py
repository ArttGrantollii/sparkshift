"""Pipeline source files, run as pipelines: each materialized view must hold
what the plain code computes for its data set, or for its query, which the
other equivalence tests check against SQL or rows written by hand.

The pipelines run on Apache Spark's Spark Declarative Pipelines, the engine
of Databricks' Lakeflow pipelines. It runs through Spark Connect, so these
tests run with SPARKSHIFT_SPARK_MODE=connect (the CI's Spark Connect job).
They call what ``spark-pipelines run`` calls: they create a dataflow graph
on the session, import the pipeline's files into it, and run it, each time
in a new schema. The test tables are stored as tables, read from their own
schema.

Apache Spark 4.2 analyzes some steps when a pipeline is defined (SPARK-59663;
docs/adr/0013-pipelines-from-the-same-plan.md): on a dataset of the same
pipeline, drop, distinct, and dropDuplicates fail, and withColumn is right
only while the dataset does not exist yet, as in a new schema. So these
tests use programs without those steps on the pipeline's own datasets; the
validation on Databricks runs every example, twice
(tools/validate_databricks.py).
"""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from compare import differences, snapshot
from datasets import TABLES

import sparkshift
from sparkshift.pipelines import pipeline_tables

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

pytestmark = [
    pytest.mark.spark,
    pytest.mark.skipif(
        os.environ.get("SPARKSHIFT_SPARK_MODE", "classic") != "connect",
        reason="Spark Declarative Pipelines run through Spark Connect: run the "
        "tests with SPARKSHIFT_SPARK_MODE=connect",
    ),
]

EXAMPLES = Path(__file__).parents[2] / "examples"
# Where the pipelines read the test tables from.
SOURCE_SCHEMA = "sparkshift_tables"


@pytest.fixture(scope="module")
def stored_tables(spark_tables: "SparkSession") -> Iterator["SparkSession"]:
    """The session, with the test tables stored in SOURCE_SCHEMA as well."""
    spark = spark_tables
    spark.sql(f"CREATE DATABASE IF NOT EXISTS {SOURCE_SCHEMA}")
    for name in TABLES:
        spark.table(name).write.mode("overwrite").saveAsTable(f"{SOURCE_SCHEMA}.{name}")
    yield spark
    spark.sql(f"DROP DATABASE {SOURCE_SCHEMA} CASCADE")


@pytest.fixture
def pipeline_schema(stored_tables: "SparkSession") -> Iterator[str]:
    """A new schema for a pipeline's datasets, dropped with its files after
    the test, so no run leaves a table location behind."""
    schema = f"pipeline_{uuid.uuid4().hex[:12]}"
    stored_tables.sql(f"CREATE DATABASE {schema}")
    yield schema
    stored_tables.sql(f"DROP DATABASE {schema} CASCADE")


def run_pipeline(
    spark: "SparkSession", files: dict[str, str], folder: Path, schema: str
) -> None:
    """Run pipeline source files as ``spark-pipelines run`` would, keeping
    the datasets in ``schema``."""
    from pyspark.pipelines.cli import LibrariesGlob, PipelineSpec, register_definitions
    from pyspark.pipelines.spark_connect_graph_element_registry import (
        SparkConnectGraphElementRegistry,
    )
    from pyspark.pipelines.spark_connect_pipeline import (
        create_dataflow_graph,
        handle_pipeline_events,
        start_run,
    )

    for name, code in files.items():
        path = folder / "transformations" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(code, encoding="utf-8")
    storage = (folder / "storage").resolve().as_uri()
    spec = PipelineSpec(
        name=schema,
        storage=storage,
        catalog=None,
        database=schema,
        configuration={},
        libraries=[LibrariesGlob(include="transformations/**")],
    )
    graph = create_dataflow_graph(
        spark, default_catalog=None, default_database=schema, sql_conf={}
    )
    registry = SparkConnectGraphElementRegistry(spark, graph)
    register_definitions(folder / "spark-pipeline.yml", registry, spec, spark, graph)
    handle_pipeline_events(
        start_run(
            spark,
            graph,
            full_refresh=None,
            full_refresh_all=False,
            refresh=None,
            dry=False,
            storage=storage,
        )
    )


def plain_datasets(
    spark: "SparkSession", result: sparkshift.ConversionResult
) -> dict[str, "DataFrame"]:
    """The DataFrame the plain code computes for each data set, by name (the
    last of each name), or for a query's result, as "result"."""
    namespace: dict[str, object] = {"spark": spark}
    exec(compile(result.code, "<sparkshift-generated>", "exec"), namespace)
    if not result.datasets:
        return {"result": namespace["result"]}  # type: ignore[dict-item]
    return {
        dataset.name: namespace[dataset.variable]  # type: ignore[misc]
        for dataset in result.datasets
    }


def assert_views_hold(
    spark: "SparkSession", schema: str, expected: dict[str, "DataFrame"]
) -> None:
    for name, dataframe in expected.items():
        actual = snapshot(spark.table(f"{schema}.{name}"))
        problems = differences(snapshot(dataframe), actual)
        assert not problems, f"{name}:\n" + "\n".join(problems)


def test_every_sql_example_in_one_pipeline(
    stored_tables: "SparkSession", tmp_path: Path, pipeline_schema: str
) -> None:
    spark = stored_tables
    files: dict[str, str] = {}
    expected: dict[str, DataFrame] = {}
    for path in sorted((EXAMPLES / "sql").rglob("*.sql")):
        dialect = None if path.parent.name == "generic" else path.parent.name
        result = sparkshift.convert(path.read_text(encoding="utf-8"), dialect=dialect)
        files[path.with_suffix(".py").name] = sparkshift.to_pipeline(
            result, source=path.name, dialect=dialect, schema=SOURCE_SCHEMA
        )
        [name] = pipeline_tables(result, source=path.name, dialect=dialect)
        expected[name] = plain_datasets(spark, result)["result"]

    schema = pipeline_schema
    run_pipeline(spark, files, tmp_path, schema)

    assert_views_hold(spark, schema, expected)


def test_every_sas_example_in_one_pipeline(
    stored_tables: "SparkSession", tmp_path: Path, pipeline_schema: str
) -> None:
    spark = stored_tables
    files: dict[str, str] = {}
    expected: dict[str, DataFrame] = {}
    temporary: list[str] = []
    for path in sorted((EXAMPLES / "sas").glob("*.sas")):
        result = sparkshift.convert(path.read_text(encoding="utf-8"), dialect="sas")
        files[path.with_suffix(".py").name] = sparkshift.to_pipeline(
            result, source=path.name, dialect="sas", schema=SOURCE_SCHEMA
        )
        datasets = plain_datasets(spark, result)
        assert not set(datasets) & set(expected), "examples share a data set name"
        expected |= datasets
        names = pipeline_tables(result, source=path.name, dialect="sas")
        temporary += [name for name in names if name not in datasets]
    assert temporary == ["customers_clean_v1"]

    schema = pipeline_schema
    run_pipeline(spark, files, tmp_path, schema)

    assert_views_hold(spark, schema, expected)
    # A temporary view is only the pipeline's.
    assert not spark.catalog.tableExists(f"{schema}.customers_clean_v1")


def test_a_file_reads_another_file_s_dataset(
    stored_tables: "SparkSession", tmp_path: Path, pipeline_schema: str
) -> None:
    spark = stored_tables
    load = sparkshift.convert(
        "data clean; set customers(keep=customer_id country score); "
        "where score ne .; run;",
        dialect="sas",
    )
    summary = sparkshift.convert(
        "SELECT country, COUNT(*) AS customers FROM clean GROUP BY country"
    )
    files = {
        "load.py": sparkshift.to_pipeline(
            load, source="load.sas", dialect="sas", schema=SOURCE_SCHEMA
        ),
        # Read without a schema: the pipeline's own dataset.
        "summary.py": sparkshift.to_pipeline(summary, source="summary.sql"),
    }

    schema = pipeline_schema
    run_pipeline(spark, files, tmp_path, schema)

    clean = plain_datasets(spark, load)["clean"]
    clean.createOrReplaceTempView("clean")
    try:
        expected = plain_datasets(spark, summary)["result"]
        assert_views_hold(spark, schema, {"clean": clean, "summary": expected})
    finally:
        spark.catalog.dropTempView("clean")


def test_versions_of_a_data_set(
    stored_tables: "SparkSession", tmp_path: Path, pipeline_schema: str
) -> None:
    # PROC SORT replaces b, so the first b is a temporary view, which the
    # sorted b reads; c reads the sorted b.
    spark = stored_tables
    program = (
        "data a; set customers(keep=customer_id score); where score ne .; run; "
        "data b; set a(keep=customer_id score); "
        "if score > 3 then grade = 'A'; else grade = 'B'; run; "
        "proc sort data=b; by customer_id; run; "
        "data c; set b; rank = customer_id - 100; run;"
    )
    result = sparkshift.convert(program, dialect="sas")
    code = sparkshift.to_pipeline(
        result, source="job.sas", dialect="sas", schema=SOURCE_SCHEMA
    )
    assert '@dp.temporary_view(name="b_v1")' in code

    schema = pipeline_schema
    run_pipeline(spark, {"job.py": code}, tmp_path, schema)

    assert_views_hold(spark, schema, plain_datasets(spark, result))
