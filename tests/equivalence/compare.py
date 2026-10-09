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
7. A LIMIT without ORDER BY may return any rows: the row count must match, and
   every returned row must exist in the unlimited result (a sub-multiset).
8. With ORDER BY, rows that tie on the sort keys may come in any order: the
   ties form groups that must appear in the same sequence, each compared as a
   multiset. With a LIMIT, only the last group may be cut short, and its rows
   must come from the same group of the unlimited result.

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
        return [_column_difference(expected, actual)]

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


def limited_differences(
    unlimited: Snapshot, actual: Snapshot, expected_count: int
) -> list[str]:
    """Compare the result of a LIMIT query that has no ORDER BY.

    Such a query may legitimately return any ``expected_count`` rows of the
    unlimited result. Instead of exact rows, check the count and that every
    returned row exists in the unlimited result, duplicates included.
    """
    if unlimited.columns != actual.columns:
        return [_column_difference(unlimited, actual)]

    problems = []
    if len(actual.rows) != expected_count:
        problems.append(f"Expected {expected_count} rows, got {len(actual.rows)}")
    not_in_reference, _ = _unmatched_rows(actual.rows, unlimited.rows)
    if not_in_reference:
        problems.append(
            _describe_rows("Rows not in the unlimited result", not_in_reference)
        )
    return problems


def ordered_differences(
    expected: Snapshot,
    actual: Snapshot,
    order_keys: Sequence[str],
    expected_count: int | None = None,
) -> list[str]:
    """Compare the result of a query with ORDER BY.

    ``expected`` is the reference result, sorted, without any LIMIT. Rows with
    equal values in the ``order_keys`` columns tie, and a correct sort may
    return tied rows in any order. ``expected_count`` is the number of rows
    the query returns with its LIMIT; None means it has no LIMIT.
    """
    if expected.columns != actual.columns:
        return [_column_difference(expected, actual)]
    names = [name for name, _ in expected.columns]
    unknown = [key for key in order_keys if key not in names]
    if unknown:
        raise ValueError(f"order_keys must be output columns: {unknown}")
    indexes = [names.index(key) for key in order_keys]

    count = len(expected.rows) if expected_count is None else expected_count
    if len(actual.rows) != count:
        return [f"Expected {count} rows, got {len(actual.rows)}"]

    start = 0
    for group in _tie_groups(expected.rows, indexes):
        if start == count:
            break
        size = min(len(group), count - start)
        chunk = actual.rows[start : start + size]
        if size == len(group):
            missing, unexpected = _unmatched_rows(group, chunk)
        else:
            # The LIMIT cut this group: any of its rows may be the ones kept.
            unexpected, _ = _unmatched_rows(chunk, group)
            missing = []
        if missing or unexpected:
            title = (
                f"Rows {start + 1} to {start + size} are out of order "
                f"(rows that tie on {', '.join(order_keys)} may come in any order)"
            )
            problems = [title]
            if missing:
                problems.append(_describe_rows("Expected at these positions", missing))
            if unexpected:
                problems.append(_describe_rows("Found instead", unexpected))
            return problems
        start += size
    return []


def _tie_groups(
    rows: Sequence[tuple[object, ...]], indexes: Sequence[int]
) -> list[list[tuple[object, ...]]]:
    """Split sorted rows into runs that are equal on the columns at ``indexes``."""
    groups: list[list[tuple[object, ...]]] = []
    for row in rows:
        first = groups[-1][0] if groups else None
        if first is not None and all(values_equal(row[i], first[i]) for i in indexes):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _column_difference(expected: Snapshot, actual: Snapshot) -> str:
    return (
        "Columns differ:\n"
        f"  expected: {_format_columns(expected.columns)}\n"
        f"  actual:   {_format_columns(actual.columns)}"
    )


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
