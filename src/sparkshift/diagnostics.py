"""Findings about a conversion, and the result returned to callers.

Errors are exceptions; warnings are data. A conversion that cannot be done
safely raises (see ``errors.UnsupportedSQLError``). A conversion that succeeds
returns a ``ConversionResult``, whose warnings describe anything the caller
should review — for example, a construct translated through a fallback.
"""

from dataclasses import dataclass

_MAX_SQL_IN_MESSAGE = 80


@dataclass(frozen=True)
class Diagnostic:
    """One finding about one SQL construct.

    ``message`` names the construct, ``sql`` is the offending SQL fragment, and
    ``hint`` optionally suggests a rewrite.
    """

    message: str
    sql: str
    hint: str | None = None

    def __str__(self) -> str:
        sql = self.sql
        if len(sql) > _MAX_SQL_IN_MESSAGE:
            sql = sql[: _MAX_SQL_IN_MESSAGE - 3] + "..."
        text = f"{self.message}: {sql}"
        if self.hint:
            text += f". Hint: {self.hint}"
        return text


@dataclass(frozen=True)
class ConversionResult:
    """The outcome of a successful conversion.

    ``code`` assigns the final DataFrame to a variable named ``result`` and
    expects a SparkSession named ``spark`` to exist, as in a Databricks notebook.
    """

    code: str
    warnings: tuple[Diagnostic, ...] = ()
