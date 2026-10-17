"""A notebook displays what its code computes, for every example.

The notebook runs as Databricks would run it: as Python, with ``spark``,
``dbutils``, and ``display`` defined. Here ``display`` records the DataFrame it
is given, and ``dbutils.widgets`` returns each widget's initial value.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from compare import differences, snapshot
from harness import run_generated

import sparkshift

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession

EXAMPLES = Path(__file__).parents[2] / "examples"
SOURCES = sorted((EXAMPLES / "sql").rglob("*.sql")) + sorted(
    (EXAMPLES / "sas").glob("*.sas")
)


def dialect_of(path: Path) -> str | None:
    if path.suffix == ".sas":
        return "sas"
    return None if path.parent.name == "generic" else path.parent.name


class Widgets:
    """Databricks' dbutils.widgets, for text widgets nobody has edited."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def text(self, name: str, default: str) -> None:
        self.values.setdefault(name, default)

    def get(self, name: str) -> str:
        return self.values[name]


class DBUtils:
    def __init__(self) -> None:
        self.widgets = Widgets()


def run_notebook(spark: "SparkSession", notebook: str) -> "DataFrame":
    """Run a notebook as Python; the DataFrame it displays."""
    displayed: list[DataFrame] = []
    namespace = {"spark": spark, "dbutils": DBUtils(), "display": displayed.append}
    exec(compile(notebook, "notebook.py", "exec"), namespace)
    [shown] = displayed
    return shown


@pytest.mark.spark
@pytest.mark.parametrize("path", SOURCES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_notebook_displays_the_result(spark_tables: "SparkSession", path: Path) -> None:
    dialect = dialect_of(path)
    result = sparkshift.convert(path.read_text(encoding="utf-8"), dialect=dialect)
    notebook = sparkshift.to_notebook(result, source=path.name, dialect=dialect)

    shown = run_notebook(spark_tables, notebook)

    expected = snapshot(run_generated(spark_tables, result.code))
    assert differences(expected, snapshot(shown)) == []


@pytest.fixture
def output_schema(spark: "SparkSession", tmp_path: Path) -> Iterator[str]:
    """An empty schema to write results to, dropped afterwards."""
    name = "sparkshift_results"
    location = (tmp_path / "results").as_uri()
    spark.sql(f"CREATE DATABASE {name} LOCATION '{location}'")
    try:
        yield name
    finally:
        spark.sql(f"DROP DATABASE IF EXISTS {name} CASCADE")


def written_tables(spark: "SparkSession", schema: str) -> list[str]:
    """The tables in a schema; Spark also lists the session's temp views."""
    tables = spark.catalog.listTables(schema)
    return sorted(table.name for table in tables if not table.isTemporary)


def computed(spark: "SparkSession", result: sparkshift.ConversionResult) -> dict:
    """What the plain code computes: each data set's DataFrame by name (the
    last of each name), or the result."""
    namespace: dict[str, object] = {"spark": spark}
    exec(compile(result.code, "<sparkshift-generated>", "exec"), namespace)
    if not result.datasets:
        return {None: namespace["result"]}
    return {dataset.name: namespace[dataset.variable] for dataset in result.datasets}


@pytest.mark.spark
@pytest.mark.parametrize("path", SOURCES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_notebook_writes_what_its_code_computes(
    spark_tables: "SparkSession", output_schema: str, path: Path
) -> None:
    dialect = dialect_of(path)
    result = sparkshift.convert(path.read_text(encoding="utf-8"), dialect=dialect)
    notebook = sparkshift.to_notebook(
        result, source=path.name, dialect=dialect, output_schema=output_schema
    )

    run_notebook(spark_tables, notebook)

    expected = {
        name or path.stem: frame
        for name, frame in computed(spark_tables, result).items()
    }
    assert written_tables(spark_tables, output_schema) == sorted(expected)
    for name, frame in expected.items():
        table = spark_tables.table(f"{output_schema}.{name}")
        assert differences(snapshot(frame), snapshot(table)) == [], name


@pytest.mark.spark
def test_each_data_set_gets_its_own_table(
    spark_tables: "SparkSession", output_schema: str
) -> None:
    # Data sets with different rows, so each table must hold its own.
    program = (
        "data big; set orders; where amount > 100; run;\n"
        "data small; set orders; where amount <= 100; run;\n"
        "proc sort data=small out=sorted; by amount; run;\n"
    )
    result = sparkshift.convert(program, dialect="sas")
    notebook = sparkshift.to_notebook(
        result, dialect="sas", output_schema=output_schema
    )

    run_notebook(spark_tables, notebook)

    frames = computed(spark_tables, result)
    assert written_tables(spark_tables, output_schema) == ["big", "small", "sorted"]
    assert snapshot(frames["big"]) != snapshot(frames["small"])
    for name, frame in frames.items():
        table = spark_tables.table(f"{output_schema}.{name}")
        assert differences(snapshot(frame), snapshot(table)) == [], name


@pytest.mark.spark
def test_written_tables_are_replaced_and_their_catalog_can_be_named(
    spark_tables: "SparkSession", output_schema: str
) -> None:
    # A table of the same name, with other columns, from an earlier run.
    spark_tables.range(5).write.saveAsTable(f"{output_schema}.names")
    result = sparkshift.convert("SELECT name FROM customers")
    notebook = sparkshift.to_notebook(
        result,
        source="names.sql",
        output_catalog="spark_catalog",
        output_schema=output_schema,
    )

    run_notebook(spark_tables, notebook)

    table = spark_tables.table(f"spark_catalog.{output_schema}.names")
    expected = spark_tables.table("customers").select("name")
    assert differences(snapshot(expected), snapshot(table)) == []


@pytest.mark.spark
def test_a_schema_name_that_needs_quoting_is_written_to(
    spark_tables: "SparkSession", tmp_path: Path
) -> None:
    # Unquoted, spark_catalog.2024.names would not parse: 2024 is a number.
    name = "2024"
    location = (tmp_path / "odd").as_uri()
    spark_tables.sql(f"CREATE DATABASE `{name}` LOCATION '{location}'")
    try:
        result = sparkshift.convert("SELECT name FROM customers")
        notebook = sparkshift.to_notebook(
            result, source="names.sql", output_schema=name
        )

        run_notebook(spark_tables, notebook)

        assert spark_tables.table(f"`{name}`.names").count() == (
            spark_tables.table("customers").count()
        )
    finally:
        spark_tables.sql(f"DROP DATABASE IF EXISTS `{name}` CASCADE")


@pytest.mark.spark
def test_nothing_is_written_without_an_output_schema(
    spark_tables: "SparkSession", output_schema: str
) -> None:
    spark_tables.catalog.setCurrentDatabase(output_schema)
    try:
        path = EXAMPLES / "sas" / "status_counts.sas"
        result = sparkshift.convert(path.read_text(encoding="utf-8"), dialect="sas")

        run_notebook(spark_tables, sparkshift.to_notebook(result, dialect="sas"))
    finally:
        spark_tables.catalog.setCurrentDatabase("default")

    assert written_tables(spark_tables, output_schema) == []


@pytest.mark.spark
def test_the_widgets_choose_the_catalog_and_schema(
    spark: "SparkSession", tmp_path: Path
) -> None:
    """The widget cell selects a schema with Spark's catalog API, in classic
    Spark and Spark Connect alike: a table only that schema has is found."""
    location = (tmp_path / "warehouse").as_uri()
    spark.sql(f"CREATE DATABASE sparkshift_widgets LOCATION '{location}'")
    try:
        spark.range(3).write.mode("overwrite").saveAsTable(
            "sparkshift_widgets.only_here"
        )
        result = sparkshift.convert("SELECT id FROM only_here")
        notebook = sparkshift.to_notebook(
            result, catalog="spark_catalog", schema="sparkshift_widgets"
        )

        shown = run_notebook(spark, notebook)

        assert sorted(row.id for row in shown.collect()) == [0, 1, 2]
        assert spark.catalog.currentDatabase() == "sparkshift_widgets"
    finally:
        spark.catalog.setCurrentDatabase("default")
        spark.sql("DROP DATABASE IF EXISTS sparkshift_widgets CASCADE")
