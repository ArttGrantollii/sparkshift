"""SparkShift: convert SQL into readable, idiomatic, tested PySpark DataFrame code."""

from sparkshift.api import convert
from sparkshift.diagnostics import ConversionResult, DataSet, Diagnostic
from sparkshift.errors import (
    MultipleStatementsError,
    SASParseError,
    SparkShiftError,
    SQLParseError,
    UnsupportedDialectError,
    UnsupportedSQLError,
)
from sparkshift.notebook import to_notebook
from sparkshift.pipelines import to_pipeline

__version__ = "0.2.0.dev0"

__all__ = [
    "ConversionResult",
    "DataSet",
    "Diagnostic",
    "MultipleStatementsError",
    "SASParseError",
    "SQLParseError",
    "SparkShiftError",
    "UnsupportedDialectError",
    "UnsupportedSQLError",
    "__version__",
    "convert",
    "to_notebook",
    "to_pipeline",
]
