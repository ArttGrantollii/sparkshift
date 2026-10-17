"""The playground's bridge, run here with CPython; in the browser, Pyodide
runs the same file (see tests/playground for the browser tests)."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

import sparkshift
from sparkshift.dialects import DIALECT_CHOICES

BRIDGE = Path(__file__).parents[1] / "playground" / "bridge.py"


@pytest.fixture(scope="module")
def bridge() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bridge", BRIDGE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_converted_query(bridge: ModuleType) -> None:
    sql = "SELECT TOP 3 name FROM customers ORDER BY name"

    result = json.loads(bridge.convert_for_page(sql, "tsql"))

    assert result == {
        "ok": True,
        "code": sparkshift.convert(sql, dialect="tsql").code,
        "warnings": [],
    }


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT TOP 3 name FROM customers ORDER BY name", "tsql"),
        ("SELECT a FROM t", ""),
        ("proc sort data=orders out=dedup nodupkey; by customer_id; run;", "sas"),
    ],
)
def test_a_notebook_is_what_the_command_line_writes(
    bridge: ModuleType, sql: str, dialect: str
) -> None:
    result = json.loads(bridge.notebook_for_page(sql, dialect))

    converted = sparkshift.convert(sql, dialect=dialect or None)
    assert result == {
        "ok": True,
        "code": sparkshift.to_notebook(converted, dialect=dialect or None),
        "warnings": [str(warning) for warning in converted.warnings],
    }


def test_a_notebook_request_reports_errors_like_code(bridge: ModuleType) -> None:
    sql = "SELECT my_udf(a) AS x FROM t"

    assert bridge.notebook_for_page(sql, "") == bridge.convert_for_page(sql, "")


def test_empty_dialect_means_generic_sql(bridge: ModuleType) -> None:
    result = json.loads(bridge.convert_for_page("SELECT a FROM t", ""))

    assert result["code"] == sparkshift.convert("SELECT a FROM t").code


def test_a_sas_program(bridge: ModuleType) -> None:
    program = "data big; set orders; where amount > 100; run;"

    result = json.loads(bridge.convert_for_page(program, "sas"))

    assert result["code"] == sparkshift.convert(program, dialect="sas").code


def test_unsupported_query_lists_each_issue(bridge: ModuleType) -> None:
    sql = "SELECT my_udf(a) AS x, CAST(b AS FLOAT) AS y FROM t"
    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        sparkshift.convert(sql)

    result = json.loads(bridge.convert_for_page(sql, ""))

    # The same text the command line prints, and each issue separately.
    assert result["ok"] is False
    assert result["error"] == str(caught.value)
    assert result["issues"] == [
        {"message": issue.message, "sql": issue.sql, "hint": issue.hint}
        for issue in caught.value.issues
    ]


@pytest.mark.parametrize(
    "sql", ["SELECT (a FROM t", "", "SELECT 1 FROM t; SELECT 2 FROM t"]
)
def test_other_errors_have_a_message_and_no_issues(
    bridge: ModuleType, sql: str
) -> None:
    with pytest.raises(sparkshift.SparkShiftError) as caught:
        sparkshift.convert(sql)

    result = json.loads(bridge.convert_for_page(sql, ""))

    assert result == {"ok": False, "error": str(caught.value), "issues": []}


def test_warnings_are_passed_on(
    bridge: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Warnings must retain the same diagnostic text as the Python API.
    warning = sparkshift.Diagnostic("fallback used", "x", "Review it.")
    monkeypatch.setattr(
        bridge.sparkshift,
        "convert",
        lambda sql, dialect: sparkshift.ConversionResult("result = x\n", (warning,)),
    )

    result = json.loads(bridge.convert_for_page("SELECT x FROM t", ""))

    assert result["warnings"] == ["fallback used: x. Hint: Review it."]


def test_about(bridge: ModuleType) -> None:
    assert json.loads(bridge.about()) == {
        "version": sparkshift.__version__,
        "dialects": list(DIALECT_CHOICES),
        "max_source_bytes": 50_000,
    }


@pytest.mark.parametrize("extra_bytes", [-1, 0, 1])
def test_source_limit_counts_utf8_bytes_before_conversion(
    bridge: ModuleType, monkeypatch: pytest.MonkeyPatch, extra_bytes: int
) -> None:
    maximum = 50_000
    prefix = "SELECT id FROM t --"
    target = maximum + extra_bytes
    sql = prefix + "é" * ((target - len(prefix)) // 2)
    sql += " " * (target - len(sql.encode("utf-8")))
    calls = []

    def convert(source: str, *, dialect: str | None) -> sparkshift.ConversionResult:
        calls.append(source)
        return sparkshift.ConversionResult("result = x\n")

    monkeypatch.setattr(bridge.sparkshift, "convert", convert)
    result = json.loads(bridge.convert_for_page(sql, ""))
    assert result["ok"] is (extra_bytes <= 0)
    assert calls == ([sql] if extra_bytes <= 0 else [])
    if extra_bytes > 0:
        assert "50,000 UTF-8 bytes" in result["error"]
        assert "CLI" in result["error"]
        assert result["issues"] == []


def test_deep_query_returns_a_readable_error(bridge: ModuleType) -> None:
    sql = "SELECT id FROM orders WHERE " + " AND ".join(
        f"id != {n}" for n in range(1000)
    )
    result = json.loads(bridge.convert_for_page(sql, ""))
    assert result["ok"] is False
    assert "deeply nested" in result["error"]
    assert "Simplify" in result["error"]
    assert result["issues"] == []


def test_unexpected_converter_errors_remain_visible_to_the_browser_boundary(
    bridge: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(sql: str, *, dialect: str | None) -> sparkshift.ConversionResult:
        raise RuntimeError("internal failure sentinel")

    monkeypatch.setattr(bridge.sparkshift, "convert", broken)
    with pytest.raises(RuntimeError, match="internal failure sentinel"):
        bridge.convert_for_page("SELECT id FROM t", "")


def test_source_limit_is_not_imposed_on_the_python_library(bridge: ModuleType) -> None:
    sql = "SELECT id FROM t --" + "x" * 50_000
    assert sparkshift.convert(sql).code == sparkshift.convert("SELECT id FROM t").code
    assert json.loads(bridge.convert_for_page(sql, ""))["ok"] is False
