"""SparkShift: convert SQL into readable, idiomatic, tested PySpark DataFrame code."""

from sparkshift.api import convert
from sparkshift.diagnostics import ConversionResult, Diagnostic
from sparkshift.errors import (
    MultipleStatementsError,
    SparkShiftError,
    SQLParseError,
    UnsupportedDialectError,
    UnsupportedSQLError,
)

__version__ = "0.1.0.dev0"

__all__ = [
    "ConversionResult",
    "Diagnostic",
    "MultipleStatementsError",
    "SQLParseError",
    "SparkShiftError",
    "UnsupportedDialectError",
    "UnsupportedSQLError",
    "__version__",
    "convert",
]
