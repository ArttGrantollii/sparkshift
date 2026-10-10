"""Exceptions raised by SparkShift.

Every error derives from SparkShiftError, so callers — user code, the CLI, the
web playground — can handle all of them in one place and never depend on the
exception types of libraries SparkShift uses internally.
"""

from collections.abc import Sequence

from sparkshift.diagnostics import Diagnostic


class SparkShiftError(Exception):
    """Base class for all SparkShift errors."""


class UnsupportedDialectError(SparkShiftError):
    """The requested SQL dialect is not one SparkShift supports."""

    def __init__(self, dialect: str, supported: Sequence[str]) -> None:
        self.dialect = dialect
        self.supported = tuple(supported)
        super().__init__(
            f"Unsupported dialect {dialect!r}. Supported dialects: "
            f"{', '.join(self.supported)}; omit the dialect for generic SQL."
        )


class SQLParseError(SparkShiftError):
    """The input is empty or is not valid SQL for the selected dialect.

    ``line`` and ``column`` are 1-based and point at the start of the offending
    token. They are ``None`` when the position is unknown.
    """

    def __init__(
        self,
        message: str,
        *,
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.message = message
        self.line = line
        self.column = column
        location = f" (line {line}, column {column})" if line is not None else ""
        super().__init__(f"{message}{location}")


class SASParseError(SQLParseError):
    """The input is empty or is not a valid SAS program.

    It is an SQLParseError too, so callers that handle invalid input handle
    both languages the same way.
    """


class UnsupportedSQLError(SparkShiftError):
    """The SQL is valid but uses constructs SparkShift cannot translate safely.

    ``issues`` lists every unsupported construct found, not just the first, so
    callers can show all of them at once.
    """

    def __init__(self, issues: Sequence[Diagnostic]) -> None:
        self.issues = tuple(issues)
        noun = "construct" if len(self.issues) == 1 else "constructs"
        details = "\n".join(f"  - {issue}" for issue in self.issues)
        super().__init__(f"{len(self.issues)} unsupported {noun}:\n{details}")


class MultipleStatementsError(SparkShiftError):
    """The input contains more than one SQL statement."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"Expected a single SQL statement but found {count}. "
            "Convert one statement at a time."
        )
