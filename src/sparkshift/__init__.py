"""SparkShift: convert SQL into readable, idiomatic, tested PySpark DataFrame code."""

from sparkshift.errors import (
    MultipleStatementsError,
    SparkShiftError,
    SQLParseError,
    UnsupportedDialectError,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "MultipleStatementsError",
    "SQLParseError",
    "SparkShiftError",
    "UnsupportedDialectError",
    "__version__",
]
