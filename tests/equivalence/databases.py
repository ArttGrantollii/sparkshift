"""Run the test tables and queries on a real database, and compare its results
with the generated PySpark's.

The helpers here need no database: they write the DDL, and make two results
from different engines comparable. Engines name and type columns their own
way, so only what the query means is compared (see docs/testing.md):

- Column names are compared ignoring case: PostgreSQL folds unquoted names
  to lower case.
- Column types are not compared; values are. In a column where either side
  has a float, all numbers are compared as floats, within the comparator's
  tolerance; otherwise, where either side has a decimal, integers are
  compared as decimals.
- A timestamp with a time zone is compared as the UTC wall-clock time, as
  Spark returns it in a session whose time zone is UTC.

A date and a timestamp never match, even at midnight: that is a real
difference in what the query returns.
"""

import datetime
from decimal import Decimal

from compare import Snapshot
from pyspark.sql.types import DataType, StructType

_POSTGRES_TYPES = {
    "boolean": "boolean",
    "int": "integer",
    "double": "double precision",
    "string": "text",
    "date": "date",
    # Spark's TIMESTAMP is an instant, PostgreSQL's TIMESTAMPTZ.
    "timestamp": "timestamptz",
    "timestamp_ntz": "timestamp",
}


def postgres_type(data_type: DataType) -> str:
    name = data_type.simpleString()
    if name.startswith("decimal("):
        return "numeric" + name.removeprefix("decimal")
    if name not in _POSTGRES_TYPES:
        raise ValueError(f"No PostgreSQL type for Spark type {name}")
    return _POSTGRES_TYPES[name]


def create_table_sql(name: str, schema: StructType) -> str:
    columns = ", ".join(
        f"{field.name} {postgres_type(field.dataType)}" for field in schema.fields
    )
    return f"CREATE TABLE {name} ({columns})"


def insert_sql(name: str, schema: StructType) -> str:
    """An INSERT with one %s placeholder per column, as psycopg expects."""
    placeholders = ", ".join(["%s"] * len(schema.fields))
    return f"INSERT INTO {name} VALUES ({placeholders})"


def comparable(expected: Snapshot, actual: Snapshot) -> tuple[Snapshot, Snapshot]:
    """Both results with lower-case column names, no types, and values in a
    form both engines share."""
    expected_rows = [tuple(map(_portable, row)) for row in expected.rows]
    actual_rows = [tuple(map(_portable, row)) for row in actual.rows]
    # The number type each column is compared as, from the values on both
    # sides; the comparator otherwise requires equal types.
    kinds: dict[int, type] = {}
    for row in [*expected_rows, *actual_rows]:
        for index, value in enumerate(row):
            if isinstance(value, float):
                kinds[index] = float
            elif isinstance(value, Decimal) and kinds.get(index) is not float:
                kinds[index] = Decimal
    return (
        _untyped(expected, _numbers_as(expected_rows, kinds)),
        _untyped(actual, _numbers_as(actual_rows, kinds)),
    )


def _portable(value: object) -> object:
    if isinstance(value, datetime.datetime) and value.tzinfo is not None:
        return value.astimezone(datetime.UTC).replace(tzinfo=None)
    return value


def _numbers_as(
    rows: list[tuple[object, ...]], kinds: dict[int, type]
) -> tuple[tuple[object, ...], ...]:
    """Rows whose integers and decimals have their column's number type."""

    def convert(value: object, index: int) -> object:
        number = isinstance(value, int | Decimal) and not isinstance(value, bool)
        kind = kinds.get(index)
        return kind(value) if number and kind is not None else value

    return tuple(
        tuple(convert(value, index) for index, value in enumerate(row)) for row in rows
    )


def _untyped(result: Snapshot, rows: tuple[tuple[object, ...], ...]) -> Snapshot:
    return Snapshot(tuple((name.lower(), "") for name, _ in result.columns), rows)
