"""Run the test tables and queries on real databases, and compare their results
with the generated PySpark's.

Each database is described by an ``Engine``: its dialect, the environment
variable holding its connection URL, and the type each Spark type maps to.
``check`` runs one query on the database and its conversion on Spark, and
compares the two.

Engines name and type results their own way, so only what the query means is
compared (see docs/testing.md):

- Column names are compared ignoring case: PostgreSQL folds unquoted names
  to lower case.
- Column types are not compared; values are. In a column where either side
  has a float, all numbers are compared as floats, within the comparator's
  tolerance; otherwise, where either side has a decimal, integers are
  compared as decimals.
- In a column where either side has a boolean, 0 and 1 are compared as false
  and true: MySQL returns comparisons as integers.
- A timestamp with a time zone is compared as the UTC wall-clock time, as
  Spark returns it in a session whose time zone is UTC.

A date and a timestamp never match, even at midnight: that is a real
difference in what the query returns.
"""

import datetime
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import test_example_queries
from compare import Snapshot, snapshot
from harness import has_order_by, result_differences, run_generated, without_limit
from pyspark.sql.types import DataType, StructType
from test_formatting import MODULES

import sparkshift

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

# Set to an engine's dialect where its tests must run, as in that engine's CI
# job: a missing URL then fails them instead of skipping them, so a green job
# always means they ran.
REQUIRED_VARIABLE = "SPARKSHIFT_DATABASE_REQUIRED"

# The schema (PostgreSQL) or database (MySQL) the tests create and drop.
TEST_SCHEMA = "sparkshift_tests"


@dataclass(frozen=True)
class Engine:
    """A database the scenarios of one dialect also run on."""

    dialect: str
    name: str
    url_variable: str
    # Spark type (simpleString) -> the engine's type; decimals keep their
    # precision and scale.
    types: Mapping[str, str]
    decimal_type: str

    def column_type(self, data_type: DataType) -> str:
        spark_type = data_type.simpleString()
        if spark_type.startswith("decimal("):
            return self.decimal_type + spark_type.removeprefix("decimal")
        if spark_type not in self.types:
            raise ValueError(f"No {self.name} type for Spark type {spark_type}")
        return self.types[spark_type]


POSTGRES = Engine(
    dialect="postgres",
    name="PostgreSQL",
    url_variable="SPARKSHIFT_POSTGRES_URL",
    types={
        "boolean": "boolean",
        "int": "integer",
        "double": "double precision",
        "string": "text",
        "date": "date",
        # Spark's TIMESTAMP is an instant, PostgreSQL's TIMESTAMPTZ.
        "timestamp": "timestamptz",
        "timestamp_ntz": "timestamp",
    },
    decimal_type="numeric",
)

MYSQL = Engine(
    dialect="mysql",
    name="MySQL",
    url_variable="SPARKSHIFT_MYSQL_URL",
    types={
        "boolean": "boolean",
        "int": "int",
        "double": "double",
        # The collation comes from the test database (see test_mysql.py).
        "string": "text",
        "date": "date",
        # MySQL's TIMESTAMP is an instant, shown in the session time zone;
        # DATETIME is a wall-clock time. Both keep microseconds with (6).
        "timestamp": "timestamp(6)",
        "timestamp_ntz": "datetime(6)",
    },
    decimal_type="decimal",
)


def skip_unless_configured(engine: Engine) -> pytest.MarkDecorator:
    configured = bool(os.environ.get(engine.url_variable))
    required = os.environ.get(REQUIRED_VARIABLE) == engine.dialect
    return pytest.mark.skipif(
        not configured and not required, reason=f"{engine.url_variable} is not set"
    )


def configured_url(engine: Engine) -> str:
    """The engine's connection URL. Never printed: it may hold a password."""
    url = os.environ.get(engine.url_variable)
    if not url:
        pytest.fail(f"{engine.url_variable} must be set when {REQUIRED_VARIABLE} is")
    return url


def create_table_sql(engine: Engine, name: str, schema: StructType) -> str:
    columns = ", ".join(
        f"{field.name} {engine.column_type(field.dataType)}" for field in schema.fields
    )
    return f"CREATE TABLE {name} ({columns})"


def insert_sql(name: str, schema: StructType) -> str:
    """An INSERT with one %s placeholder per column, as psycopg and PyMySQL
    expect."""
    placeholders = ", ".join(["%s"] * len(schema.fields))
    return f"INSERT INTO {name} VALUES ({placeholders})"


def cases(dialect: str) -> list[Any]:
    """Every dialect scenario and example of one dialect, with its ORDER BY
    keys, as pytest parameters."""
    found = []
    for module in MODULES:
        for name, (scenario_dialect, sql, _reference, *rest) in getattr(
            module, "DIALECT_SCENARIOS", {}
        ).items():
            if scenario_dialect == dialect:
                order_keys = rest[0] if rest else None
                found.append(pytest.param(sql, order_keys, id=name))
    examples = test_example_queries.EXAMPLES
    for name, (_reference, order_keys) in test_example_queries.REFERENCES.items():
        if name.startswith(f"{dialect}/"):
            sql = (examples / f"{name}.sql").read_text(encoding="utf-8")
            found.append(pytest.param(sql, order_keys, id=name))
    return found


def run_query(connection: Any, sql: str) -> Snapshot:
    """Run a query with a DB-API connection and return its result."""
    with connection.cursor() as cursor:
        cursor.execute(sql)
        columns = tuple((_column_name(column), "") for column in cursor.description)
        return Snapshot(columns, tuple(tuple(row) for row in cursor.fetchall()))


def _column_name(column: Any) -> str:
    # psycopg describes a column with an object, PyMySQL with a tuple.
    return str(column.name if hasattr(column, "name") else column[0])


def check(
    run: Callable[[str], Snapshot],
    spark: "SparkSession",
    sql: str,
    dialect: str,
    order_keys: list[str] | None,
) -> None:
    """Assert that ``sql``, run on the database by ``run``, returns what its
    conversion returns on Spark."""
    if has_order_by(sql, dialect) != (order_keys is not None):
        raise ValueError("Pass order_keys exactly when the query has ORDER BY.")
    code = sparkshift.convert(sql, dialect=dialect).code
    actual = snapshot(run_generated(spark, code))

    unlimited = without_limit(sql, dialect)
    expected = run(unlimited or sql)
    expected_count = None if unlimited is None else len(run(sql).rows)
    expected, actual = comparable(expected, actual)
    problems = result_differences(expected, actual, order_keys, expected_count)

    context = [f"Query:\n{sql}", f"Generated code:\n{code}"]
    assert not problems, "\n\n".join([*context, *problems])


def comparable(expected: Snapshot, actual: Snapshot) -> tuple[Snapshot, Snapshot]:
    """Both results with lower-case column names, no types, and values in a
    form both engines share."""
    expected_rows = [tuple(map(_portable, row)) for row in expected.rows]
    actual_rows = [tuple(map(_portable, row)) for row in actual.rows]
    # The type each column's numbers are compared as, from the values on both
    # sides; the comparator otherwise requires equal types.
    kinds: dict[int, type] = {}
    for row in [*expected_rows, *actual_rows]:
        for index, value in enumerate(row):
            if isinstance(value, bool):
                kinds[index] = bool
            elif isinstance(value, float) and kinds.get(index) is not bool:
                kinds[index] = float
            elif isinstance(value, Decimal) and kinds.get(index) not in (bool, float):
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
    """Rows whose numbers have their column's type: a boolean column's 0 and 1
    become false and true; other numbers become floats or decimals."""

    def convert(value: object, index: int) -> object:
        if isinstance(value, bool) or not isinstance(value, int | Decimal):
            return value
        kind = kinds.get(index)
        if kind is bool:
            return bool(value) if value in (0, 1) else value
        return kind(value) if kind is not None else value

    return tuple(
        tuple(convert(value, index) for index, value in enumerate(row)) for row in rows
    )


def _untyped(result: Snapshot, rows: tuple[tuple[object, ...], ...]) -> Snapshot:
    return Snapshot(tuple((name.lower(), "") for name, _ in result.columns), rows)
