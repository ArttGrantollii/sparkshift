"""Coverage reports: how much of a set of SQL files SparkShift converts, and
what blocks the rest.

``assess`` converts one file's SQL and records its outcome; a ``Report``
collects the outcomes and ranks the constructs that block conversion. The
formatters render a report as text for a terminal, Markdown for a pull
request or wiki, or JSON for other tools. Nothing here reads or writes files.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from sparkshift.api import convert
from sparkshift.diagnostics import Diagnostic
from sparkshift.errors import (
    MultipleStatementsError,
    SQLParseError,
    UnsupportedSQLError,
)

# Increased whenever the JSON shape changes incompatibly.
JSON_VERSION = 1

SEVERAL_STATEMENTS = "several statements in one file"


class Status(Enum):
    """What happened to one file. Every file has exactly one status."""

    CONVERTED = "converted"
    CONVERTED_WITH_WARNINGS = "converted_with_warnings"
    NOT_CONVERTED = "not_converted"
    INVALID_SQL = "invalid_sql"
    UNREADABLE = "unreadable"


_LABELS = {
    Status.CONVERTED: "Converted",
    Status.CONVERTED_WITH_WARNINGS: "Converted with warnings",
    Status.NOT_CONVERTED: "Not converted",
    Status.INVALID_SQL: "Invalid SQL",
    Status.UNREADABLE: "Unreadable",
}


@dataclass(frozen=True)
class FileOutcome:
    """The outcome for one file.

    ``findings`` are the constructs that block conversion, or a converted
    file's warnings. ``error`` says why invalid SQL or an unreadable file
    could not be assessed.
    """

    path: str
    status: Status
    findings: tuple[Diagnostic, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class Blocker:
    """A construct that blocks conversion, with how often it does."""

    construct: str
    hint: str | None
    files: int
    occurrences: int
    example: str


def assess(path: str, sql: str, dialect: str | None = None) -> FileOutcome:
    """Convert one file's SQL and record the outcome."""
    try:
        result = convert(sql, dialect=dialect)
    except UnsupportedSQLError as error:
        return FileOutcome(path, Status.NOT_CONVERTED, error.issues)
    except MultipleStatementsError as error:
        finding = Diagnostic(
            SEVERAL_STATEMENTS,
            f"{error.count} statements",
            hint="Put each statement in its own file.",
        )
        return FileOutcome(path, Status.NOT_CONVERTED, (finding,))
    except SQLParseError as error:
        return FileOutcome(path, Status.INVALID_SQL, error=str(error))
    if result.warnings:
        return FileOutcome(path, Status.CONVERTED_WITH_WARNINGS, result.warnings)
    return FileOutcome(path, Status.CONVERTED)


def unreadable(path: str, reason: str) -> FileOutcome:
    return FileOutcome(path, Status.UNREADABLE, error=reason)


@dataclass(frozen=True)
class Report:
    """The outcomes for a set of files, in the order they were assessed."""

    source: str
    dialect: str | None
    outcomes: tuple[FileOutcome, ...]

    def __post_init__(self) -> None:
        if not self.outcomes:
            raise ValueError("A report needs at least one file")

    @property
    def total(self) -> int:
        return len(self.outcomes)

    def count(self, *statuses: Status) -> int:
        return sum(outcome.status in statuses for outcome in self.outcomes)

    @property
    def converted(self) -> int:
        """Files converted, with or without warnings."""
        return self.count(Status.CONVERTED, Status.CONVERTED_WITH_WARNINGS)

    @property
    def converted_percent(self) -> Decimal:
        return percent(self.converted, self.total)

    @property
    def blockers(self) -> tuple[Blocker, ...]:
        """Constructs that block conversion, the ones blocking the most files
        first, then the most frequent, then by name."""
        files: dict[str, list[str]] = {}
        occurrences: dict[str, int] = {}
        hints: dict[str, str | None] = {}
        for outcome in self.outcomes:
            if outcome.status is not Status.NOT_CONVERTED:
                continue
            for finding in outcome.findings:
                construct = finding.message
                paths = files.setdefault(construct, [])
                if outcome.path not in paths:
                    paths.append(outcome.path)
                occurrences[construct] = occurrences.get(construct, 0) + 1
                hints.setdefault(construct, finding.hint)
        blockers = [
            Blocker(
                construct,
                hints[construct],
                len(paths),
                occurrences[construct],
                paths[0],
            )
            for construct, paths in files.items()
        ]
        return tuple(sorted(blockers, key=_blocker_order))


def _blocker_order(blocker: Blocker) -> tuple[int, int, str, str]:
    """Most files first, then most occurrences, then alphabetical, ignoring
    case ("function X" and "OFFSET clause" sort as words, not by capitals)."""
    construct = blocker.construct
    return (-blocker.files, -blocker.occurrences, construct.casefold(), construct)


def percent(part: int, whole: int) -> Decimal:
    """``part`` as a percentage of ``whole``, rounded half up to one decimal,
    so the same counts always print the same number."""
    exact = Decimal(100 * part) / Decimal(whole)
    return exact.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


# --- Formats -------------------------------------------------------------------


def to_text(report: Report) -> str:
    """A report for a terminal: totals, blockers, and files that could not be
    assessed."""
    lines = [f"SparkShift coverage report: {report.source} ({_dialect(report)})", ""]
    rows = [("Files", str(report.total), "")]
    for label, count, nested in _totals(report):
        label = f"  {label}" if nested else _sentence(label)
        rows.append((label, str(count), f"{percent(count, report.total)}%"))
    lines += _columns(rows, right=(1, 2))

    blockers = report.blockers
    if blockers:
        lines += ["", "What blocks conversion"]
        table = [("Construct", "Files", "Occurrences")]
        table += [(b.construct, str(b.files), str(b.occurrences)) for b in blockers]
        lines += _columns(table, right=(1, 2))
    for status in (Status.INVALID_SQL, Status.UNREADABLE):
        failed = [o for o in report.outcomes if o.status is status]
        if failed:
            lines += ["", _LABELS[status]]
            lines += [f"  {o.path}: {_one_line(o.error or '')}" for o in failed]
    if blockers:
        lines += ["", "Hints and example files: --format markdown or --format json."]
    return "\n".join(lines) + "\n"


def to_markdown(report: Report) -> str:
    """A report as Markdown tables, for a pull request or a wiki."""
    lines = [
        "# SparkShift coverage report",
        "",
        f"Source: `{report.source}`. Dialect: {_dialect(report)}.",
        "",
        "| Status | Files | Share |",
        "|---|---:|---:|",
    ]
    for label, count, nested in _totals(report):
        label = f"of which {label}" if nested else _sentence(label)
        lines.append(f"| {label} | {count} | {percent(count, report.total)}% |")
    lines.append(f"| **Total** | **{report.total}** | |")

    blockers = report.blockers
    if blockers:
        lines += [
            "",
            "## What blocks conversion",
            "",
            "| Construct | Files | Occurrences | Example | Hint |",
            "|---|---:|---:|---|---|",
        ]
        lines += [
            f"| {_cell(b.construct)} | {b.files} | {b.occurrences} | "
            f"`{_cell(b.example)}` | {_cell(b.hint or '')} |"
            for b in blockers
        ]
    lines += ["", "## Files", "", "| File | Status | Details |", "|---|---|---|"]
    for outcome in report.outcomes:
        details = outcome.error or "; ".join(f.message for f in outcome.findings)
        lines.append(
            f"| `{_cell(outcome.path)}` | {_LABELS[outcome.status]} | "
            f"{_cell(details)} |"
        )
    return "\n".join(lines) + "\n"


def to_json(report: Report) -> str:
    """A report as JSON with a stable shape (see ``JSON_VERSION``)."""
    data = {
        "version": JSON_VERSION,
        "source": report.source,
        "dialect": report.dialect,
        "totals": {
            "files": report.total,
            **{status.value: report.count(status) for status in Status},
            "converted_percent": float(report.converted_percent),
        },
        "blockers": [
            {
                "construct": b.construct,
                "hint": b.hint,
                "files": b.files,
                "occurrences": b.occurrences,
                "example": b.example,
            }
            for b in report.blockers
        ],
        "files": [
            {
                "path": outcome.path,
                "status": outcome.status.value,
                "findings": [
                    {"message": f.message, "sql": f.sql, "hint": f.hint}
                    for f in outcome.findings
                ],
                "error": outcome.error,
            }
            for outcome in report.outcomes
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _totals(report: Report) -> list[tuple[str, int, bool]]:
    """Rows (label, count, nested) whose top-level rows add up to the number
    of files: converted, then each kind of failure. The nested row counts the
    converted files that have warnings."""
    return [
        ("converted", report.converted, False),
        ("with warnings", report.count(Status.CONVERTED_WITH_WARNINGS), True),
        ("not converted", report.count(Status.NOT_CONVERTED), False),
        ("invalid SQL", report.count(Status.INVALID_SQL), False),
        ("unreadable", report.count(Status.UNREADABLE), False),
    ]


def _sentence(label: str) -> str:
    """The label with its first letter in upper case ("invalid SQL" becomes
    "Invalid SQL", where str.capitalize would give "Invalid sql")."""
    return label[0].upper() + label[1:]


def _dialect(report: Report) -> str:
    return report.dialect or "generic SQL"


def _columns(rows: Sequence[tuple[str, ...]], right: tuple[int, ...]) -> list[str]:
    """Align rows into columns two spaces apart; ``right`` columns are
    right-aligned."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return [
        "  ".join(
            cell.rjust(width) if i in right else cell.ljust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=True))
        ).rstrip()
        for row in rows
    ]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _cell(text: str) -> str:
    """Text safe inside a Markdown table cell."""
    return _one_line(text).replace("|", "\\|")
