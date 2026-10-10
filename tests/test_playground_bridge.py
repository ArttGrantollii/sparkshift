"""The playground's bridge, run here with CPython; in the browser, Pyodide
runs the same file (see tests/playground for the browser tests)."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

import sparkshift
from sparkshift.dialects import SUPPORTED_DIALECTS

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


def test_empty_dialect_means_generic_sql(bridge: ModuleType) -> None:
    result = json.loads(bridge.convert_for_page("SELECT a FROM t", ""))

    assert result["code"] == sparkshift.convert("SELECT a FROM t").code


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
    # No conversion produces warnings yet; the page must still show them.
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
        "dialects": list(SUPPORTED_DIALECTS),
    }
