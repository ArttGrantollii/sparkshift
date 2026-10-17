"""The playground's link to SparkShift, run in the browser by Pyodide.

The page calls ``convert_for_page`` and gets JSON it can show without knowing
SparkShift's exception types. Errors carry the same text the command line
prints, so the playground and the CLI always agree.
"""

import json

import sparkshift
from sparkshift.dialects import DIALECT_CHOICES

# A playground policy, not a limit imposed by the library or CLI.
MAX_SOURCE_BYTES = 50_000


def convert_for_page(sql: str, dialect: str) -> str:
    """Convert ``sql`` (``dialect`` "" for generic SQL, "sas" for a SAS
    program) and return JSON:

    ``{"ok": true, "code": ..., "warnings": [...]}`` on success, or
    ``{"ok": false, "error": ..., "issues": [...]}`` when it cannot be
    converted; ``issues`` lists each unsupported construct separately.
    """
    return _convert(sql, dialect, notebook=False)


def notebook_for_page(sql: str, dialect: str) -> str:
    """As ``convert_for_page``, with the code written as a Databricks
    notebook, exactly as ``sparkshift convert --notebook`` writes it."""
    return _convert(sql, dialect, notebook=True)


def _convert(sql: str, dialect: str, *, notebook: bool) -> str:
    if len(sql.encode("utf-8")) > MAX_SOURCE_BYTES:
        return json.dumps(
            {
                "ok": False,
                "error": f"The browser demo accepts up to {MAX_SOURCE_BYTES:,} "
                "UTF-8 bytes of source code. Use the CLI for larger files.",
                "issues": [],
            }
        )
    try:
        result = sparkshift.convert(sql, dialect=dialect or None)
    except RecursionError:
        return json.dumps(
            {
                "ok": False,
                "error": "This input is too deeply nested for the browser converter. "
                "Simplify expressions or split it into smaller queries.",
                "issues": [],
            }
        )
    except sparkshift.SparkShiftError as error:
        issues = getattr(error, "issues", ())
        return json.dumps(
            {
                "ok": False,
                "error": str(error),
                "issues": [_issue(issue) for issue in issues],
            }
        )
    code = (
        sparkshift.to_notebook(result, dialect=dialect or None)
        if notebook
        else result.code
    )
    return json.dumps(
        {
            "ok": True,
            "code": code,
            "warnings": [str(warning) for warning in result.warnings],
        }
    )


def about() -> str:
    """JSON with the SparkShift version and the dialects the page can offer,
    SAS last."""
    return json.dumps(
        {
            "version": sparkshift.__version__,
            "dialects": list(DIALECT_CHOICES),
            "max_source_bytes": MAX_SOURCE_BYTES,
        }
    )


def _issue(issue: sparkshift.Diagnostic) -> dict[str, str | None]:
    return {"message": issue.message, "sql": issue.sql, "hint": issue.hint}
