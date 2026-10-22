"""Tests for tools/validate_databricks.py, without a workspace.

The tool itself runs only by hand, with credentials; these tests keep its
offline parts working: the canonical values and the driver notebook.
"""

import ast
import datetime
import importlib.util
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

# Building the driver reads the test tables, whose schemas are PySpark types.
pytestmark = pytest.mark.spark

TOOL = Path(__file__).parents[1] / "tools" / "validate_databricks.py"


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("validate_databricks", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (7, 7),
        ("Zoë", "Zoë"),
        (Decimal("120.50"), "120.50"),  # exact, with its scale
        (1 / 3, "0.3333333333"),  # doubles to 10 digits
        (datetime.date(2024, 2, 29), "2024-02-29"),
        (datetime.datetime(2024, 1, 3, 9, 15), "2024-01-03T09:15:00"),
    ],
)
def test_canonical_values(tool: ModuleType, value: object, expected: object) -> None:
    assert tool.canonical(value) == expected


def test_every_example_is_found_with_its_dialect(tool: ModuleType) -> None:
    found = {name: dialect for name, _, dialect in tool.examples()}

    assert found["sql/generic/revenue_ranking"] is None
    assert found["sql/tsql/top_customers"] == "tsql"
    assert found["sas/status_counts"] == "sas"
    assert len(found) == len(
        list((tool.EXAMPLES / "sql").glob("*/*.sql"))
        + list((tool.EXAMPLES / "sas").glob("*.sas"))
    )


def test_the_driver_runs_and_checks_every_example(tool: ModuleType) -> None:
    expected = {name: ("struct<x:int>", ["[1]"]) for name, _, _ in tool.examples()}

    source = tool.driver_source(expected, "workspace", "sparkshift")

    assert source.startswith("# Databricks notebook source\n")
    ast.parse(source)  # valid Python; %run lines are MAGIC comments
    cells = source.split("\n# COMMAND ----------\n")
    runs = [cell.strip() for cell in cells if "%run" in cell]
    assert runs == [f"# MAGIC %run ./examples/{name}" for name in expected]
    for name in expected:
        assert f"_validation_check({name!r}, result)" in source
    assert 'saveAsTable("workspace.sparkshift." + _name)' in source
    # The examples' own widgets select the schema, not the driver.
    assert "USE " not in source


def test_the_driver_checks_the_tables_each_example_writes(tool: ModuleType) -> None:
    expected = {name: ("struct<x:int>", ["[1]"]) for name, _, _ in tool.examples()}
    written = {name: {"t1": ("struct<x:int>", ["[1]"])} for name in expected}

    source = tool.driver_source(
        expected, "workspace", "sparkshift", written, "workspace.results"
    )

    ast.parse(source)
    assert 'spark.sql("CREATE SCHEMA IF NOT EXISTS workspace.results")' in source
    # Every table exists first, with other columns, so writes replace it.
    assert "spark.range(1).write.mode('overwrite')" in source
    for name in expected:
        assert (
            f"_validation_check({name!r}, result)\n_validation_check_tables({name!r})"
        ) in source
    without = tool.driver_source(expected, "c", "s")
    assert "\n_validation_check_tables(" not in without
    assert "CREATE SCHEMA IF NOT EXISTS workspace.results" not in without


def test_the_driver_check_reports_differences(tool: ModuleType) -> None:
    """Run the driver's setup cell with a fake DataFrame: the comparison
    must pass on equal results and report each kind of difference."""
    expected = {"ok": ("struct<x:int>", ['["a"]']), "bad": ("struct<x:int>", ['["a"]'])}
    setup = tool.driver_source(expected, "c", "s").split("# COMMAND ----------")[0]

    class Frame:
        def __init__(self, rows: list[tuple[object, ...]], types: str) -> None:
            self.rows, self.schema = (
                rows,
                type("S", (), {"simpleString": lambda _: types})(),
            )

        def collect(self) -> list[tuple[object, ...]]:
            return self.rows

    scope: dict[str, object] = {
        "spark": type(
            "Spark", (), {"conf": type("C", (), {"set": lambda *_: None})()}
        )()
    }
    exec(setup, scope)
    check = scope["_validation_check"]
    check("ok", Frame([("a",)], "struct<x:int>"))  # type: ignore[operator]
    check("bad", Frame([("a",), ("b",)], "struct<x:bigint>"))  # type: ignore[operator]

    # A table is checked against its own expected rows.
    check("ok -> t", Frame([("a",)], "struct<x:int>"), ("struct<x:int>", ['["a"]']))  # type: ignore[operator]

    ok, bad, table = scope["_validation_report"]  # type: ignore[misc]
    assert table == {"example": "ok -> t", "status": "pass"}
    assert ok == {"example": "ok", "status": "pass"}
    assert bad["status"] == "fail"
    assert bad["types"] == ["struct<x:int>", "struct<x:bigint>"]
    assert bad["unexpected"] == ['["b"]']
    assert bad["counts"] == [1, 2]


# --- Pipelines -------------------------------------------------------------------


def test_the_tables_notebook_loads_the_test_tables(tool: ModuleType) -> None:
    source = tool.tables_source("workspace", "sparkshift", "workspace.pipeline")

    ast.parse(source)
    assert 'spark.sql("CREATE SCHEMA IF NOT EXISTS workspace.sparkshift")' in source
    assert 'spark.sql("CREATE SCHEMA IF NOT EXISTS workspace.pipeline")' in source
    assert 'saveAsTable("workspace.sparkshift." + _name)' in source


def test_the_pipeline_check_compares_each_materialized_view(tool: ModuleType) -> None:
    expected = {"ok": ("struct<x:int>", ['["a"]']), "bad": ("struct<x:int>", ['["a"]'])}
    source = tool.check_source(expected, "workspace.pipeline")
    ast.parse(source)

    class Frame:
        def __init__(self, rows: list[tuple[object, ...]], types: str) -> None:
            self.rows, self.schema = (
                rows,
                type("S", (), {"simpleString": lambda _: types})(),
            )

        def collect(self) -> list[tuple[object, ...]]:
            return self.rows

    frames = {
        "workspace.pipeline.ok": Frame([("a",)], "struct<x:int>"),
        "workspace.pipeline.bad": Frame([("a",), ("b",)], "struct<x:int>"),
    }
    exits: list[str] = []
    spark = type(
        "Spark",
        (),
        {
            "conf": type("C", (), {"set": lambda *_: None})(),
            "table": staticmethod(lambda name: frames[name]),
            "version": "4.2.0",
        },
    )()
    notebook = type("N", (), {"exit": staticmethod(exits.append)})()
    scope: dict[str, object] = {
        "spark": spark,
        "dbutils": type("D", (), {"notebook": notebook})(),
    }
    exec(source.removeprefix("# Databricks notebook source\n"), scope)

    import json

    [report] = [json.loads(text) for text in exits]
    ok, bad = report["results"]
    assert ok == {"example": "ok", "status": "pass"}
    assert bad["status"] == "fail"
    assert bad["unexpected"] == ['["b"]']
    assert bad["counts"] == [1, 2]


def test_the_pipeline_program_runs_the_steps_spark_analyzes_early(
    tool: ModuleType,
) -> None:
    import sparkshift
    from sparkshift.pipelines import pipeline_tables

    name, program = tool.PIPELINE_PROGRAM
    result = sparkshift.convert(program, dialect="sas")
    code = sparkshift.to_pipeline(result, source="pipeline_steps.sas", dialect="sas")

    assert name == "sas/pipeline_steps"
    assert pipeline_tables(result, dialect="sas") == [
        "scored",
        "trimmed",
        "unique_scores",
        "countries",
    ]
    for step in ('.drop("country")', ".dropDuplicates(", ".distinct()"):
        assert step in code
