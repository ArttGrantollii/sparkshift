"""The playground's link to SparkShift, run in the browser by Pyodide.

The page calls ``convert_for_page`` and gets JSON it can show without knowing
SparkShift's exception types. Errors carry the same text the command line
prints, so the playground and the CLI always agree.
"""

import json

import sparkshift
from sparkshift.dialects import SUPPORTED_DIALECTS


def convert_for_page(sql: str, dialect: str) -> str:
    """Convert ``sql`` (``dialect`` "" for generic SQL) and return JSON:

    ``{"ok": true, "code": ..., "warnings": [...]}`` on success, or
    ``{"ok": false, "error": ..., "issues": [...]}`` when it cannot be
    converted; ``issues`` lists each unsupported construct separately.
    """
    try:
        result = sparkshift.convert(sql, dialect=dialect or None)
    except sparkshift.SparkShiftError as error:
        issues = getattr(error, "issues", ())
        return json.dumps(
            {
                "ok": False,
                "error": str(error),
                "issues": [_issue(issue) for issue in issues],
            }
        )
    return json.dumps(
        {
            "ok": True,
            "code": result.code,
            "warnings": [str(warning) for warning in result.warnings],
        }
    )


def about() -> str:
    """JSON with the SparkShift version and the dialects the page can offer."""
    return json.dumps(
        {"version": sparkshift.__version__, "dialects": list(SUPPORTED_DIALECTS)}
    )


def _issue(issue: sparkshift.Diagnostic) -> dict[str, str | None]:
    return {"message": issue.message, "sql": issue.sql, "hint": issue.hint}
