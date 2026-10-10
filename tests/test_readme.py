"""Keep the README's examples identical to what SparkShift actually produces."""

import io
import sys
from pathlib import Path

import pytest

import sparkshift
from sparkshift.cli import main

README = (Path(__file__).parents[1] / "README.md").read_text()


def test_usage_example_output_matches_real_output() -> None:
    sql = "SELECT order_id, amount * 1.10 AS with_tax FROM sales.orders"

    assert f'"{sql}"' in README
    assert f"```python\n{sparkshift.convert(sql).code}```" in README


def test_command_line_example_matches_real_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sql = "SELECT TOP 3 name FROM customers ORDER BY name"
    monkeypatch.setattr(sys, "stdin", io.StringIO(sql))

    assert main(["convert", "-", "--dialect", "tsql"]) == 0

    command = f'echo "{sql}" | sparkshift convert - --dialect tsql'
    assert command in README
    assert f"```python\n{capsys.readouterr().out}```" in README


def test_unsupported_example_matches_real_error() -> None:
    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        sparkshift.convert(
            "SELECT name, my_udf(score) AS s FROM customers LIMIT 10 OFFSET 20"
        )

    assert f"UnsupportedSQLError: {caught.value}" in README
