"""The public conversion entry point: SQL text in, PySpark code out."""

from sparkshift.diagnostics import ConversionResult
from sparkshift.dialects import resolve_dialect
from sparkshift.emit import emit
from sparkshift.parsing import parse_sql
from sparkshift.translate import translate


def convert(sql: str, dialect: str | None = None) -> ConversionResult:
    """Convert one SQL query into PySpark DataFrame code.

    ``dialect`` names the SQL dialect of the input (for example ``"tsql"`` or
    ``"snowflake"``); omit it for generic SQL.

    Raises:
        UnsupportedDialectError: ``dialect`` is not supported.
        SQLParseError: the input is empty or not valid SQL.
        MultipleStatementsError: the input contains more than one statement.
        UnsupportedSQLError: the query uses constructs SparkShift cannot
            translate safely; ``issues`` lists all of them.
    """
    read = resolve_dialect(dialect)
    tree = parse_sql(sql, read)
    plan = translate(tree, read)
    return ConversionResult(code=emit(plan))
