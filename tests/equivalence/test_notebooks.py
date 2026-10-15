"""A notebook displays what its code computes, for every example.

The notebook runs as Databricks would run it: as Python, with ``spark``,
``dbutils``, and ``display`` defined. Here ``display`` records the DataFrame it
is given, and ``dbutils.widgets`` returns each widget's initial value.
"""

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
