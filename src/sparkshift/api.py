"""The public conversion entry point: SQL text in, PySpark code out."""

from sparkshift.diagnostics import ConversionResult
from sparkshift.dialects import is_sas, resolve_dialect
from sparkshift.emit import emit, emit_datasets
from sparkshift.parsing import parse_sql
from sparkshift.sas_parser import parse_sas
from sparkshift.sas_translate import translate_sas
from sparkshift.translate import translate


def convert(sql: str, dialect: str | None = None) -> ConversionResult:
    """Convert one SQL query, or a SAS program, into PySpark DataFrame code.

    ``dialect`` names the SQL dialect of the input (for example ``"tsql"`` or
    ``"snowflake"``); omit it for generic SQL. With ``"sas"``, the input is a
    SAS program: each data set it creates becomes a DataFrame variable, and
    ``result`` is the last one.

    Raises:
        UnsupportedDialectError: ``dialect`` is not supported.
        SQLParseError: the input is empty or not valid SQL (SASParseError, a
            subclass, for SAS).
        MultipleStatementsError: the SQL contains more than one statement.
        UnsupportedSQLError: the input uses constructs SparkShift cannot
            translate safely; ``issues`` lists all of them.
    """
    if is_sas(dialect):
        translation = translate_sas(parse_sas(sql), sql)
        return ConversionResult(
            code=emit_datasets(translation.datasets), warnings=translation.warnings
        )
    read = resolve_dialect(dialect)
    tree = parse_sql(sql, read)
    plan = translate(tree, read)
    return ConversionResult(code=emit(plan))
