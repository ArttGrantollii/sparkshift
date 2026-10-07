"""SQL dialects SparkShift accepts as input."""

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


def resolve_dialect(dialect: str | None) -> str | None:
    """Validate a dialect name and return it in the form SQLGlot expects.

    ``None`` selects SQLGlot's generic dialect, which accepts common ANSI-style
    SQL. Names are case-insensitive.

    Raises:
        UnsupportedDialectError: ``dialect`` is not a supported dialect name.
    """
    if dialect is None:
        return None
    normalized = dialect.strip().lower()
    if normalized not in SUPPORTED_DIALECTS:
        raise UnsupportedDialectError(dialect, SUPPORTED_DIALECTS)
    return normalized
