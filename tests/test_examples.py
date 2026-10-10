"""The examples gallery stays exactly what SparkShift generates today.

Whether each example also returns the same result as its SQL on Spark is
checked in tests/equivalence/test_example_queries.py.
"""

import importlib.util
from pathlib import Path

import pytest

import sparkshift
from sparkshift.dialects import SUPPORTED_DIALECTS

EXAMPLES = Path(__file__).parents[1] / "examples"
SQL_FILES = sorted((EXAMPLES / "sql").rglob("*.sql"))
REGENERATE = "Run `uv run python examples/regenerate.py` and review the diff."


def dialect_of(sql_path: Path) -> str | None:
    folder = sql_path.parent.name
    return None if folder == "generic" else folder


def generated_path(sql_path: Path, root: Path = EXAMPLES / "pyspark") -> Path:
    return root / sql_path.parent.name / sql_path.with_suffix(".py").name


def example_id(sql_path: Path) -> str:
    return f"{sql_path.parent.name}/{sql_path.stem}"


@pytest.mark.parametrize("sql_path", SQL_FILES, ids=example_id)
def test_generated_example_is_up_to_date(sql_path: Path) -> None:
    sql = sql_path.read_text(encoding="utf-8")
    expected = sparkshift.convert(sql, dialect=dialect_of(sql_path)).code

    actual = generated_path(sql_path).read_text(encoding="utf-8")

    assert actual == expected, REGENERATE


@pytest.mark.parametrize("sql_path", SQL_FILES, ids=example_id)
def test_generated_example_fits_the_line_length(sql_path: Path) -> None:
    lines = generated_path(sql_path).read_text(encoding="utf-8").splitlines()

    assert max(len(line) for line in lines) <= 88


def test_every_generated_file_has_its_query() -> None:
    generated = sorted((EXAMPLES / "pyspark").rglob("*.py"))

    assert generated == [generated_path(path) for path in SQL_FILES]


def test_examples_cover_generic_sql_and_every_dialect() -> None:
    assert {path.parent.name for path in SQL_FILES} == {"generic", *SUPPORTED_DIALECTS}


def test_readme_links_every_example() -> None:
    readme = (EXAMPLES / "README.md").read_text(encoding="utf-8")

    for sql_path in SQL_FILES:
        folder, stem = sql_path.parent.name, sql_path.stem
        assert f"(sql/{folder}/{stem}.sql)" in readme
        assert f"(pyspark/{folder}/{stem}.py)" in readme


def test_regenerate_script_reproduces_the_committed_files(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "regenerate", EXAMPLES / "regenerate.py"
    )
    assert spec is not None and spec.loader is not None
    regenerate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(regenerate)

    assert regenerate.regenerate(tmp_path) == 0

    for sql_path in SQL_FILES:
        assert generated_path(sql_path, tmp_path).read_bytes() == (
            generated_path(sql_path).read_bytes()
        )
