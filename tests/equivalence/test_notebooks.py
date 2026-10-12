"""A notebook displays what its code computes, for every example.

The notebook runs as Databricks would run it: as Python, with ``spark`` and
``display`` defined. Here ``display`` records the DataFrame it is given.
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


@pytest.mark.spark
@pytest.mark.parametrize("path", SOURCES, ids=lambda p: f"{p.parent.name}/{p.stem}")
def test_notebook_displays_the_result(spark_tables: "SparkSession", path: Path) -> None:
    dialect = dialect_of(path)
    result = sparkshift.convert(path.read_text(encoding="utf-8"), dialect=dialect)
    notebook = sparkshift.to_notebook(result, source=path.name, dialect=dialect)
    displayed: list[DataFrame] = []

    namespace = {"spark": spark_tables, "display": displayed.append}
    exec(compile(notebook, path.name, "exec"), namespace)

    [shown] = displayed
    expected = snapshot(run_generated(spark_tables, result.code))
    assert differences(expected, snapshot(shown)) == []
