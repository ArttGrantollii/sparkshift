"""Compare two query results the way SQL semantics require.

The rules, and why each exists, are documented in docs/testing.md:

1. Row order is ignored: without ORDER BY, SQL guarantees no order.
2. Rows are compared as a multiset: duplicates must match in number.
3. Column names, order, and types must match exactly.
4. Column nullability flags are ignored: Spark derives them differently per
   code path; actual NULL values are still compared.
5. NULL matches NULL.
6. Floating-point values match within a tolerance; all other values match
   exactly.

The comparison itself works on plain Python data (``Snapshot``), so it can be
tested without starting Spark.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyspark.sql import DataFrame

FLOAT_REL_TOL = 1e-9
FLOAT_ABS_TOL = 1e-12
_MAX_ROWS_SHOWN = 10


@dataclass(frozen=True)
class Snapshot:
    """A query result as plain Python data."""

    # (name, Spark type) pairs, e.g. ("amount", "decimal(10,2)").
    columns: tuple[tuple[str, str], ...]
    rows: tuple[tuple[object, ...], ...]


def snapshot(df: "DataFrame") -> Snapshot:
    columns = tuple(
        (field.name, field.dataType.simpleString()) for field in df.schema.fields
    )
    rows = tuple(tuple(row) for row in df.collect())
    return Snapshot(columns, rows)


def differences(expected: Snapshot, actual: Snapshot) -> list[str]:
    """Return human-readable differences; an empty list means equivalent."""
    if expected.columns != actual.columns:
        return [
            "Columns differ:\n"
            f"  expected: {_format_columns(expected.columns)}\n"
            f"  actual:   {_format_columns(actual.columns)}"
        ]

    missing, unexpected = _unmatched_rows(expected.rows, actual.rows)
    problems = []
    if missing:
        problems.append(
            _describe_rows("Rows missing from the generated result", missing)
        )
    if unexpected:
        problems.append(
            _describe_rows("Unexpected rows in the generated result", unexpected)
        )
    return problems


def _unmatched_rows(
    expected: Sequence[tuple[object, ...]],
    actual: Sequence[tuple[object, ...]],
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    """Match rows one-to-one, ignoring order; return the leftovers on each side.

    Each actual row can match at most one expected row, so duplicate counts
    must agree. Matching is quadratic, which is fine for small test datasets.
    """
    remaining = list(actual)
    missing = []
    for row in expected:
        for index, candidate in enumerate(remaining):
            if values_equal(row, candidate):
                del remaining[index]
                break
        else:
            missing.append(row)
    return missing, remaining


def values_equal(a: object, b: object) -> bool:
    """Compare two values from a result: NULL matches NULL, floats approximately."""
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return math.isclose(a, b, rel_tol=FLOAT_REL_TOL, abs_tol=FLOAT_ABS_TOL)
    if isinstance(a, tuple | list) and isinstance(b, tuple | list):
        # Rows, structs, and arrays: compare element by element.
        return len(a) == len(b) and all(
            values_equal(x, y) for x, y in zip(a, b, strict=True)
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(values_equal(a[key], b[key]) for key in a)
    return type(a) is type(b) and a == b


def _format_columns(columns: tuple[tuple[str, str], ...]) -> str:
    return ", ".join(f"{name} {type_}" for name, type_ in columns)


def _describe_rows(title: str, rows: list[tuple[object, ...]]) -> str:
    lines = [f"{title} ({len(rows)}):"]
    lines += [f"  {row!r}" for row in rows[:_MAX_ROWS_SHOWN]]
    if len(rows) > _MAX_ROWS_SHOWN:
        lines.append(f"  ... and {len(rows) - _MAX_ROWS_SHOWN} more")
    return "\n".join(lines)
