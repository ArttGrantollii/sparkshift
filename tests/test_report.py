"""Tests for coverage reports, without files or the command line."""

import itertools
import json
from decimal import Decimal

import pytest

import sparkshift.report
from sparkshift.diagnostics import ConversionResult, Diagnostic
from sparkshift.report import (
    JSON_VERSION,
    SEVERAL_STATEMENTS,
    Blocker,
    FileOutcome,
    Report,
    Status,
    assess,
    percent,
    to_json,
    to_markdown,
    to_text,
    unreadable,
)

UDF = Diagnostic("function MY_UDF", "MY_UDF(a)")
OFFSET = Diagnostic("OFFSET clause", "OFFSET 5", hint="Use a row limit.")
WARNING = Diagnostic("approximate translation", "x")


# --- Assessing one file --------------------------------------------------------


def test_convertible_sql_is_converted() -> None:
    assert assess("a.sql", "SELECT a FROM t") == FileOutcome("a.sql", Status.CONVERTED)


def test_unsupported_sql_lists_every_construct() -> None:
    outcome = assess("a.sql", "SELECT my_udf(a) AS x FROM t LIMIT 1 OFFSET 2")

    assert outcome.status is Status.NOT_CONVERTED
    assert [f.message for f in outcome.findings] == ["function MY_UDF", "OFFSET clause"]


def test_several_statements_are_a_blocker() -> None:
    outcome = assess("a.sql", "SELECT a FROM t; SELECT b FROM u")

    assert outcome.status is Status.NOT_CONVERTED
    assert outcome.findings == (
        Diagnostic(
            SEVERAL_STATEMENTS, "2 statements", "Put each statement in its own file."
        ),
    )


@pytest.mark.parametrize("sql", ["", "SELECT a FROM t WHERE", "SELEC a FROM t"])
def test_invalid_sql_records_the_parse_error(sql: str) -> None:
    outcome = assess("a.sql", sql)

    assert outcome.status is Status.INVALID_SQL
    assert outcome.error
    assert outcome.findings == ()


def test_the_dialect_is_used() -> None:
    sql = "SELECT TOP 3 a FROM t"

    assert assess("a.sql", sql, "tsql").status is Status.CONVERTED
    assert assess("a.sql", sql, "postgres").status is Status.INVALID_SQL


def test_warnings_make_a_separate_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def convert_with_warning(sql: str, dialect: str | None = None) -> ConversionResult:
        return ConversionResult("result = t\n", (WARNING,))

    monkeypatch.setattr(sparkshift.report, "convert", convert_with_warning)

    assert assess("a.sql", "SELECT a FROM t") == FileOutcome(
        "a.sql", Status.CONVERTED_WITH_WARNINGS, (WARNING,)
    )


def test_unreadable_files_keep_the_reason() -> None:
    assert unreadable("a.sql", "it is not UTF-8 text") == FileOutcome(
        "a.sql", Status.UNREADABLE, error="it is not UTF-8 text"
    )


# --- Counting ------------------------------------------------------------------


def _outcome(status: Status, path: str = "f.sql") -> FileOutcome:
    findings = {
        Status.NOT_CONVERTED: (UDF,),
        Status.CONVERTED_WITH_WARNINGS: (WARNING,),
    }
    errors = {Status.INVALID_SQL: "bad", Status.UNREADABLE: "unreadable"}
    return FileOutcome(path, status, findings.get(status, ()), errors.get(status))


@pytest.mark.parametrize(
    "statuses", list(itertools.combinations_with_replacement(Status, 3))
)
def test_every_file_is_counted_once(statuses: tuple[Status, ...]) -> None:
    report = Report("q", None, tuple(_outcome(s) for s in statuses))

    assert sum(report.count(status) for status in Status) == report.total == 3
    assert report.converted == sum(
        s in (Status.CONVERTED, Status.CONVERTED_WITH_WARNINGS) for s in statuses
    )
    totals = json.loads(to_json(report))["totals"]
    assert sum(totals[status.value] for status in Status) == totals["files"]


def test_a_report_needs_files() -> None:
    with pytest.raises(ValueError, match="at least one file"):
        Report("q", None, ())


@pytest.mark.parametrize(
    ("part", "whole", "expected"),
    [
        (0, 7, "0.0"),
        (7, 7, "100.0"),
        (1, 3, "33.3"),
        (2, 3, "66.7"),
        (1, 8, "12.5"),
        # Exactly 6.25: half up gives 6.3, where binary floats round to 6.2.
        (1, 16, "6.3"),
        (1, 400, "0.3"),
    ],
)
def test_percentages_round_half_up(part: int, whole: int, expected: str) -> None:
    assert percent(part, whole) == Decimal(expected)


# --- Blockers ------------------------------------------------------------------


def test_blockers_count_files_and_occurrences() -> None:
    report = Report(
        "q",
        None,
        (
            FileOutcome("a.sql", Status.NOT_CONVERTED, (UDF, UDF, OFFSET)),
            FileOutcome("b.sql", Status.NOT_CONVERTED, (UDF,)),
            FileOutcome("c.sql", Status.CONVERTED),
        ),
    )

    assert report.blockers == (
        Blocker("function MY_UDF", None, files=2, occurrences=3, example="a.sql"),
        Blocker(
            "OFFSET clause", "Use a row limit.", files=1, occurrences=1, example="a.sql"
        ),
    )


def test_warnings_are_not_blockers() -> None:
    report = Report("q", None, (_outcome(Status.CONVERTED_WITH_WARNINGS),))

    assert report.blockers == ()


def test_blockers_rank_by_files_then_occurrences_then_name() -> None:
    def blocked(path: str, *messages: str) -> FileOutcome:
        findings = tuple(Diagnostic(message, "x") for message in messages)
        return FileOutcome(path, Status.NOT_CONVERTED, findings)

    report = Report(
        "q",
        None,
        (
            blocked("1.sql", "zeta", "Beta", "alpha", "often", "often", "often"),
            blocked("2.sql", "zeta", "Beta", "alpha"),
            blocked("3.sql", "zeta"),
        ),
    )

    # zeta blocks 3 files; alpha and Beta 2 each, by name ignoring case; often
    # blocks 1 file, 3 times.
    assert [b.construct for b in report.blockers] == ["zeta", "alpha", "Beta", "often"]


# --- Formats -------------------------------------------------------------------

SAMPLE = Report(
    "queries/",
    "snowflake",
    (
        FileOutcome("a.sql", Status.NOT_CONVERTED, (UDF, OFFSET)),
        FileOutcome("b.sql", Status.CONVERTED),
        FileOutcome("c.sql", Status.CONVERTED_WITH_WARNINGS, (WARNING,)),
        FileOutcome("d.sql", Status.INVALID_SQL, error="Missing FROM\n(line 1)"),
        FileOutcome("e.sql", Status.UNREADABLE, error="it is not UTF-8 text"),
    ),
)


def test_text_format() -> None:
    assert to_text(SAMPLE) == (
        "SparkShift coverage report: queries/ (snowflake)\n"
        "\n"
        "Files            5\n"
        "Converted        2  40.0%\n"
        "  with warnings  1  20.0%\n"
        "Not converted    1  20.0%\n"
        "Invalid SQL      1  20.0%\n"
        "Unreadable       1  20.0%\n"
        "\n"
        "What blocks conversion\n"
        "Construct        Files  Occurrences\n"
        "function MY_UDF      1            1\n"
        "OFFSET clause        1            1\n"
        "\n"
        "Invalid SQL\n"
        "  d.sql: Missing FROM (line 1)\n"
        "\n"
        "Unreadable\n"
        "  e.sql: it is not UTF-8 text\n"
        "\n"
        "Hints and example files: --format markdown or --format json.\n"
    )


def test_text_format_without_blockers_or_failures() -> None:
    report = Report("q.sql", None, (_outcome(Status.CONVERTED),))

    assert to_text(report) == (
        "SparkShift coverage report: q.sql (generic SQL)\n"
        "\n"
        "Files            1\n"
        "Converted        1  100.0%\n"
        "  with warnings  0    0.0%\n"
        "Not converted    0    0.0%\n"
        "Invalid SQL      0    0.0%\n"
        "Unreadable       0    0.0%\n"
    )


def test_markdown_format() -> None:
    markdown = to_markdown(SAMPLE)

    assert markdown.startswith(
        "# SparkShift coverage report\n"
        "\n"
        "Source: `queries/`. Dialect: snowflake.\n"
        "\n"
        "| Status | Files | Share |\n"
        "|---|---:|---:|\n"
        "| Converted | 2 | 40.0% |\n"
        "| of which with warnings | 1 | 20.0% |\n"
    )
    assert "| **Total** | **5** | |\n" in markdown
    assert "| OFFSET clause | 1 | 1 | `a.sql` | Use a row limit. |\n" in markdown
    assert "| `a.sql` | Not converted | function MY_UDF; OFFSET clause |\n" in markdown
    assert (
        "| `c.sql` | Converted with warnings | approximate translation |\n" in markdown
    )
    assert "| `d.sql` | Invalid SQL | Missing FROM (line 1) |\n" in markdown


def test_markdown_without_blockers_has_no_blocker_table() -> None:
    report = Report("q", None, (_outcome(Status.CONVERTED, "a.sql"),))

    markdown = to_markdown(report)

    assert "What blocks conversion" not in markdown
    assert markdown.endswith(
        "| **Total** | **1** | |\n"
        "\n"
        "## Files\n"
        "\n"
        "| File | Status | Details |\n"
        "|---|---|---|\n"
        "| `a.sql` | Converted |  |\n"
    )


def test_markdown_cells_escape_pipes() -> None:
    finding = Diagnostic("a || b in MySQL", "a || b", hint="Use OR | CONCAT")
    report = Report(
        "q", None, (FileOutcome("a|b.sql", Status.NOT_CONVERTED, (finding,)),)
    )

    markdown = to_markdown(report)

    assert (
        "| a \\|\\| b in MySQL | 1 | 1 | `a\\|b.sql` | Use OR \\| CONCAT |" in markdown
    )


def test_json_format() -> None:
    data = json.loads(to_json(SAMPLE))

    assert data["version"] == JSON_VERSION
    assert data["source"] == "queries/"
    assert data["dialect"] == "snowflake"
    assert data["totals"] == {
        "files": 5,
        "converted": 1,
        "converted_with_warnings": 1,
        "not_converted": 1,
        "invalid_sql": 1,
        "unreadable": 1,
        "converted_percent": 40.0,
    }
    assert data["blockers"][1] == {
        "construct": "OFFSET clause",
        "hint": "Use a row limit.",
        "files": 1,
        "occurrences": 1,
        "example": "a.sql",
    }
    assert data["files"][0] == {
        "path": "a.sql",
        "status": "not_converted",
        "findings": [
            {"message": "function MY_UDF", "sql": "MY_UDF(a)", "hint": None},
            {"message": "OFFSET clause", "sql": "OFFSET 5", "hint": "Use a row limit."},
        ],
        "error": None,
    }
    assert [f["status"] for f in data["files"]] == [
        "not_converted",
        "converted",
        "converted_with_warnings",
        "invalid_sql",
        "unreadable",
    ]
    assert data["files"][3]["error"] == "Missing FROM\n(line 1)"


def test_json_keeps_non_ascii_text() -> None:
    report = Report("q", None, (FileOutcome("café.sql", Status.CONVERTED),))

    assert '"café.sql"' in to_json(report)
