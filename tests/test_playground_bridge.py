"""The playground's bridge, run here with CPython; in the browser, Pyodide
runs the same file (see tests/playground for the browser tests)."""

import base64
import importlib.util
import io
import json
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

import sparkshift
from sparkshift.dialects import DIALECT_CHOICES
from sparkshift.notebook import markdown_text, notebook_cells

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
    cells = notebook_cells(converted, dialect=dialect or None)
    assert result == {
        "ok": True,
        "code": sparkshift.to_notebook(converted, dialect=dialect or None),
        "warnings": [str(warning) for warning in converted.warnings],
        "cells": [
            {
                "kind": cell.kind,
                "title": cell.title,
                "text": markdown_text(cell.source)
                if cell.kind == "markdown"
                else cell.source,
            }
            for cell in cells
        ],
    }


def test_plain_code_has_no_cells(bridge: ModuleType) -> None:
    assert "cells" not in json.loads(bridge.convert_for_page("SELECT a FROM t", ""))


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
        "max_batch_files": 200,
    }


def test_a_notebook_for_deploying_starts_at_the_given_locations(
    bridge: ModuleType,
) -> None:
    sql = "SELECT a FROM t"
    locations = ("workspace", "raw", "main", "claims")

    result = json.loads(bridge.notebook_for_page(sql, "", "load.sql", *locations))

    assert result["code"] == sparkshift.to_notebook(
        sparkshift.convert(sql),
        source="load.sql",
        catalog="workspace",
        schema="raw",
        output_catalog="main",
        output_schema="claims",
    )


FOLDER = "/Workspace/Users/me@example.com/claims"


def test_the_job_notebook_is_the_one_upload_would_create(bridge: ModuleType) -> None:
    from sparkshift.jobs import create_job_notebook, job_settings

    notebooks = [f"{FOLDER}/load", f"{FOLDER}/legacy/report"]
    locations = ("workspace", "raw", "", "claims")

    result = json.loads(
        bridge.job_for_page("", FOLDER, json.dumps(notebooks), *locations)
    )

    settings = job_settings(
        "sparkshift claims",
        FOLDER,
        notebooks,
        catalog="workspace",
        schema="raw",
        output_schema="claims",
    )
    assert result == {
        "ok": True,
        "name": "sparkshift claims",
        "notebook": create_job_notebook(settings),
    }
    named = json.loads(bridge.job_for_page(" nightly ", FOLDER, "[]", "", "", "", ""))
    assert named["name"] == "nightly"


@pytest.mark.parametrize(
    ("folder", "notebooks"),
    [
        ("relative/folder", ["relative/folder/a"]),
        ("/Workspace/../Shared", ["/Workspace/../Shared/a"]),
        (FOLDER, ["/Workspace/Shared/elsewhere"]),
        (FOLDER, [f"{FOLDER}/../escape"]),
        (FOLDER, [f"{FOLDER}//a"]),
    ],
)
def test_a_job_needs_notebooks_inside_an_absolute_folder(
    bridge: ModuleType, folder: str, notebooks: list[str]
) -> None:
    result = json.loads(
        bridge.job_for_page("", folder, json.dumps(notebooks), "", "", "", "")
    )

    assert result["ok"] is False
    assert "absolute path" in result["error"]


def test_a_notebook_is_titled_after_its_source_file(bridge: ModuleType) -> None:
    sql = "SELECT a FROM t"

    result = json.loads(bridge.notebook_for_page(sql, "postgres", "daily.sql"))

    converted = sparkshift.convert(sql, dialect="postgres")
    assert result["code"] == sparkshift.to_notebook(
        converted, source="daily.sql", dialect="postgres"
    )


# --- Folders -------------------------------------------------------------------

COVERAGE = Path(__file__).parents[1] / "examples" / "coverage"


def chosen_folder(folder: Path) -> list[str]:
    """The paths a browser reports for a chosen folder: every file, relative
    to the folder's parent, in no particular order."""
    return [
        path.relative_to(folder.parent).as_posix()
        for path in reversed(sorted(folder.rglob("*")))
        if path.is_file()
    ]


def plan(bridge: ModuleType, paths: list[str], dialect: str = "", **options: bool):
    return json.loads(
        bridge.plan_for_page(json.dumps(paths), dialect, options.get("notebook", False))
    )


def test_a_folder_is_planned_as_the_command_line_converts_it(
    bridge: ModuleType,
) -> None:
    paths = chosen_folder(COVERAGE)

    result = plan(bridge, paths, "snowflake")

    sources = sorted(
        path
        for path in COVERAGE.rglob("*")
        if path.suffix in (".sql", ".sas") and path.is_file()
    )
    assert result["ok"] is True
    assert result["folder"] == "coverage_pyspark"
    assert [file["name"] for file in result["files"]] == [
        path.relative_to(COVERAGE).as_posix() for path in sources
    ]
    assert [file["output"] for file in result["files"]] == [
        path.relative_to(COVERAGE).with_suffix(".py").as_posix() for path in sources
    ]
    assert [paths[file["index"]] for file in result["files"]] == [
        f"coverage/{file['name']}" for file in result["files"]
    ]
    assert {file["dialect"] for file in result["files"]} == {"snowflake"}
    assert result["skipped"] == len(paths) - len(sources)


def test_sas_files_are_always_sas_and_the_sas_dialect_selects_only_them(
    bridge: ModuleType,
) -> None:
    paths = ["jobs/load.sql", "jobs/report.SAS", "jobs/notes.txt"]

    mixed = plan(bridge, paths, "tsql")
    sas_only = plan(bridge, paths, "sas")

    assert [(f["name"], f["dialect"]) for f in mixed["files"]] == [
        ("load.sql", "tsql"),
        ("report.SAS", "sas"),
    ]
    assert mixed["skipped"] == 1
    assert [(f["name"], f["dialect"]) for f in sas_only["files"]] == [
        ("report.SAS", "sas")
    ]
    assert sas_only["skipped"] == 2


def test_files_chosen_one_by_one_keep_their_names(bridge: ModuleType) -> None:
    result = plan(bridge, ["b.sql", "a.sql"], notebook=True)

    assert result["folder"] == "converted_notebooks"
    assert [(f["name"], f["output"]) for f in result["files"]] == [
        ("a.sql", "a.py"),
        ("b.sql", "b.py"),
    ]


@pytest.mark.parametrize(
    ("paths", "colliding"),
    [
        (["q/report.sql", "q/Report.sas", "q/other.sql"], ["Report.sas", "report.sql"]),
        (["q/a.sql", "q/a.py/x.sql"], ["a.py/x.sql", "a.sql"]),
    ],
)
def test_colliding_outputs_convert_nothing(
    bridge: ModuleType, paths: list[str], colliding: list[str]
) -> None:
    result = plan(bridge, paths)

    assert result["ok"] is False
    assert "Nothing was converted" in result["error"]
    assert [collision["name"] for collision in result["collisions"]] == colliding


@pytest.mark.parametrize(
    ("paths", "dialect", "error"),
    [
        (["docs/readme.md"], "", "There are no .sql or .sas files to convert."),
        (
            ["docs/readme.md", "docs/load.sql"],
            "sas",
            "There are no .sas files to convert.",
        ),
        ([], "", "There are no .sql or .sas files to convert."),
    ],
)
def test_nothing_to_convert(
    bridge: ModuleType, paths: list[str], dialect: str, error: str
) -> None:
    result = plan(bridge, paths, dialect)

    assert result == {"ok": False, "error": error, "issues": []}


def test_a_batch_is_limited(bridge: ModuleType) -> None:
    exactly = plan(bridge, [f"q/{n}.sql" for n in range(200)])
    over = plan(bridge, [f"q/{n}.sql" for n in range(201)])

    assert exactly["ok"] is True
    assert over["ok"] is False
    assert "up to 200 files" in over["error"]
    assert "CLI" in over["error"]


@pytest.mark.parametrize(
    "path", ["../a.sql", "/a.sql", "q//a.sql", "q/./a.sql", "q/../a.sql", "q\\a.sql"]
)
def test_unusable_paths_are_refused(bridge: ModuleType, path: str) -> None:
    result = plan(bridge, ["q/b.sql", path])

    assert result["ok"] is False
    assert result["issues"] == []


def test_the_zip_holds_what_the_command_line_writes(
    bridge: ModuleType, tmp_path: Path
) -> None:
    from sparkshift.cli import main

    written = tmp_path / "cli"
    main(["convert", str(COVERAGE), "-o", str(written), "-d", "snowflake"])

    planned = plan(bridge, chosen_folder(COVERAGE), "snowflake")
    converted = []
    for file in planned["files"]:
        sql = (COVERAGE / file["name"]).read_text(encoding="utf-8")
        result = json.loads(bridge.convert_for_page(sql, file["dialect"]))
        if result["ok"]:
            converted.append({"output": file["output"], "code": result["code"]})
    archive = zipfile.ZipFile(io.BytesIO(packed(bridge, "coverage_pyspark", converted)))

    expected = {
        f"coverage_pyspark/{path.relative_to(written).as_posix()}": path.read_bytes()
        for path in written.rglob("*")
        if path.is_file()
    }
    assert {name: archive.read(name) for name in archive.namelist()} == expected
    # Some file fails, so the comparison covers files that get no output.
    assert len(converted) < len(planned["files"])


def packed(bridge: ModuleType, folder: str, files: list[dict[str, str]]) -> bytes:
    result = json.loads(bridge.zip_for_page(folder, json.dumps(files)))
    assert result["ok"] is True
    return base64.b64decode(result["zip"])


def test_the_same_files_make_the_same_zip(bridge: ModuleType) -> None:
    files = [
        {"output": "a.py", "code": "result = x\n"},
        {"output": "b/c.py", "code": "é\n"},
    ]

    first = packed(bridge, "converted_pyspark", files)

    assert packed(bridge, "converted_pyspark", files) == first
    archive = zipfile.ZipFile(io.BytesIO(first))
    assert archive.namelist() == ["converted_pyspark/a.py", "converted_pyspark/b/c.py"]
    assert archive.read("converted_pyspark/b/c.py") == "é\n".encode()


@pytest.mark.parametrize("output", ["../a.py", "/a.py", "b/../../a.py"])
def test_the_zip_refuses_paths_outside_its_folder(
    bridge: ModuleType, output: str
) -> None:
    result = json.loads(
        bridge.zip_for_page(
            "converted_pyspark", json.dumps([{"output": output, "code": ""}])
        )
    )

    assert result["ok"] is False


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
