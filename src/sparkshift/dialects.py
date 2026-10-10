"""SQL dialects SparkShift accepts as input, and SAS."""

from sparkshift.errors import UnsupportedDialectError

# Dialects SparkShift accepts, using SQLGlot's dialect names. SQLGlot knows
# many more; we only list the ones we test, so we never claim untested support.
SUPPORTED_DIALECTS: tuple[str, ...] = (
    "tsql",
    "postgres",
    "mysql",
    "snowflake",
    "bigquery",
    "oracle",
)

# SAS is a different language with its own front end, not a SQL dialect, but
# callers choose it the same way: convert(program, dialect="sas").
SAS = "sas"

# Every name a caller may pass as the dialect.
DIALECT_CHOICES: tuple[str, ...] = (*SUPPORTED_DIALECTS, SAS)


def is_sas(dialect: str | None) -> bool:
    return dialect is not None and dialect.strip().lower() == SAS


def resolve_dialect(dialect: str | None) -> str | None:
    """Validate a SQL dialect name and return it in the form SQLGlot expects.

    ``None`` selects SQLGlot's generic dialect, which accepts common ANSI-style
    SQL. Names are case-insensitive.

    Raises:
        UnsupportedDialectError: ``dialect`` is not a supported dialect name.
    """
    if dialect is None:
        return None
    normalized = dialect.strip().lower()
    if normalized not in SUPPORTED_DIALECTS:
        raise UnsupportedDialectError(dialect, DIALECT_CHOICES)
    return normalized
