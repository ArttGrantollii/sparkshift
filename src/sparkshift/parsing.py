"""Parse SQL text into a SQLGlot syntax tree.

This is the only module that calls SQLGlot's parser. It translates SQLGlot's
exceptions into SparkShift's own, so no SQLGlot error type reaches callers.
"""

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from sparkshift.dialects import resolve_dialect
from sparkshift.errors import MultipleStatementsError, SQLParseError

# SQLGlot error descriptions can embed Python internals. These patterns turn
# them into user-facing text:
#   "Expected table name but got <Token token_type: TokenType.SENTINEL, ...>"
#   "Required keyword: 'this' missing for <class 'sqlglot.expressions.query.Where'>"
_TOKEN_REPR = re.compile(
    r"<Token token_type: TokenType\.(?P<type>\w+), text: (?P<text>.*?), line: .*?>"
)
_MISSING_PART = re.compile(
    r"Required keyword: '\w+' missing for <class '[\w.]+\.(?P<node>\w+)'>"
)


def parse_sql(sql: str, dialect: str | None = None) -> exp.Expression:
    """Parse exactly one SQL statement and return its syntax tree.

    Raises:
        UnsupportedDialectError: ``dialect`` is not supported.
        SQLParseError: the input is empty or is not valid SQL for the dialect.
        MultipleStatementsError: the input contains more than one statement.
    """
    read = resolve_dialect(dialect)
    try:
        parsed = sqlglot.parse(sql, read=read)
    except ParseError as error:
        raise _to_sql_parse_error(error) from error
    except TokenError as error:
        # Raised before parsing starts, without position information.
        raise SQLParseError(
            "Could not read the SQL text. "
            "Check for an unterminated string or quoted identifier."
        ) from error

    # SQLGlot yields None for empty statements (blank input, stray semicolons)
    # and a Semicolon node for a comment that follows the final semicolon.
    statements = [
        statement
        for statement in parsed
        if statement is not None and not isinstance(statement, exp.Semicolon)
    ]
    if not statements:
        raise SQLParseError("No SQL statement found.")
    if len(statements) > 1:
        raise MultipleStatementsError(len(statements))
    return statements[0]


def _to_sql_parse_error(error: ParseError) -> SQLParseError:
    if not error.errors:
        return SQLParseError("Invalid SQL.")

    # SQLGlot stops at the first error by default, so the first entry is the
    # one that matters.
    details = error.errors[0]
    message = details.get("description") or "Invalid SQL"
    message = _TOKEN_REPR.sub(_describe_token, message)
    message = _MISSING_PART.sub(_describe_missing_part, message)
    highlight = details.get("highlight") or ""
    if highlight:
        message = f"{message} near {highlight!r}"

    line = details.get("line")
    column = details.get("col")
    # SQLGlot reports the column where the offending token ends; callers
    # (editors, the web playground) need where it starts.
    if column is not None and highlight and "\n" not in highlight:
        column = column - len(highlight) + 1
    return SQLParseError(message, line=line, column=column)


def _describe_token(match: re.Match[str]) -> str:
    if match["type"] == "SENTINEL":
        return "end of input"
    return repr(match["text"])


def _describe_missing_part(match: re.Match[str]) -> str:
    return f"Incomplete {match['node'].upper()}"
