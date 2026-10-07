"""Generate PySpark source code from SparkShift's IR.

The emitter only knows the IR and how to write readable Python. It must not
import SQLGlot or PySpark.
"""

import re
from typing import assert_never

from sparkshift.ir import Relation, TableScan

RESULT_VARIABLE = "result"

# Identifier parts Spark accepts without backtick quoting.
_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def emit(plan: Relation) -> str:
    """Return PySpark code that assigns the plan's DataFrame to ``result``."""
    return f"{RESULT_VARIABLE} = {_relation(plan)}\n"


def _relation(plan: Relation) -> str:
    match plan:
        case TableScan(name_parts=parts):
            return f"spark.table({python_string(spark_table_name(parts))})"
    # Type checkers flag this line if a Relation type is not handled above.
    assert_never(plan)


def spark_table_name(parts: tuple[str, ...]) -> str:
    """Join name parts into a Spark multipart identifier.

    Parts that are not plain identifiers are wrapped in backticks, with any
    backtick inside doubled, e.g. ``("sales", "my table")`` becomes
    ``sales.`my table```.
    """
    return ".".join(
        part if _PLAIN_IDENTIFIER.fullmatch(part) else f"`{part.replace('`', '``')}`"
        for part in parts
    )


def python_string(value: str) -> str:
    """Return a double-quoted Python string literal for ``value``."""
    escaped = []
    for char in value:
        if char in ('"', "\\"):
            escaped.append("\\" + char)
        elif char.isprintable():
            escaped.append(char)
        else:
            # repr() yields a valid escape such as \n, \t, or \x00.
            escaped.append(repr(char)[1:-1])
    return '"' + "".join(escaped) + '"'
