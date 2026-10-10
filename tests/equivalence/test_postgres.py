"""PostgreSQL scenarios on a real PostgreSQL server.

The dialect scenarios check SparkShift's PySpark against Spark SQL written by
hand to mean what the PostgreSQL query means. These tests remove that
assumption: they run the original query on PostgreSQL itself and compare its
rows with the generated PySpark's on Spark.

They need a server: set SPARKSHIFT_POSTGRES_URL to a connection URL for a
database the tests may write to. CI starts a throwaway PostgreSQL container
(see .github/workflows/ci.yml); without the variable these tests are skipped.
The tables are created in their own schema, which is dropped first.
"""

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest
import test_example_queries
from compare import Snapshot, snapshot
from databases import comparable, create_table_sql, insert_sql
from datasets import TABLES
from harness import has_order_by, result_differences, run_generated, without_limit
from test_formatting import MODULES

import sparkshift

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

URL_VARIABLE = "SPARKSHIFT_POSTGRES_URL"
# Set where the tests must run, as in CI's PostgreSQL job: a missing URL then
# fails them instead of skipping them, so a green job always means they ran.
REQUIRED_VARIABLE = "SPARKSHIFT_DATABASE_REQUIRED"
SCHEMA = "sparkshift_tests"

pytestmark = [
    pytest.mark.spark,
    pytest.mark.database,
    pytest.mark.skipif(
        not os.environ.get(URL_VARIABLE) and not os.environ.get(REQUIRED_VARIABLE),
        reason=f"{URL_VARIABLE} is not set",
    ),
]


def postgres_cases() -> list[Any]:
    """Every PostgreSQL dialect scenario, and every PostgreSQL example, with
    its ORDER BY keys."""
    cases = []
    for module in MODULES:
        for name, (dialect, sql, _reference, *rest) in getattr(
            module, "DIALECT_SCENARIOS", {}
        ).items():
            if dialect == "postgres":
                order_keys = rest[0] if rest else None
                cases.append(pytest.param(sql, order_keys, id=name))
    examples = test_example_queries.EXAMPLES
    for name, (_reference, order_keys) in test_example_queries.REFERENCES.items():
        if name.startswith("postgres/"):
            sql = (examples / f"{name}.sql").read_text(encoding="utf-8")
            cases.append(pytest.param(sql, order_keys, id=name))
    return cases


@pytest.fixture(scope="module")
def postgres() -> Iterator[Any]:
    """A connection to PostgreSQL with the test tables loaded, in UTC."""
    import psycopg

    url = os.environ.get(URL_VARIABLE)
    if not url:
        pytest.fail(f"{URL_VARIABLE} must be set when {REQUIRED_VARIABLE} is")
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("SET TIME ZONE 'UTC'")
        connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        connection.execute(f"CREATE SCHEMA {SCHEMA}")
        connection.execute(f"SET search_path TO {SCHEMA}")
        for name, (schema, rows) in TABLES.items():
            connection.execute(create_table_sql(name, schema))
            with connection.cursor() as cursor:
                cursor.executemany(insert_sql(name, schema), rows)
        yield connection
        connection.execute(f"DROP SCHEMA {SCHEMA} CASCADE")


def _run(connection: Any, sql: str) -> Snapshot:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        columns = tuple((column.name, "") for column in cursor.description)
        return Snapshot(columns, tuple(tuple(row) for row in cursor.fetchall()))


@pytest.mark.parametrize(("sql", "order_keys"), postgres_cases())
def test_postgres_returns_what_the_pyspark_returns(
    postgres: Any,
    spark_tables: "SparkSession",
    sql: str,
    order_keys: list[str] | None,
) -> None:
    if has_order_by(sql, "postgres") != (order_keys is not None):
        raise ValueError("Pass order_keys exactly when the query has ORDER BY.")
    code = sparkshift.convert(sql, dialect="postgres").code
    actual = snapshot(run_generated(spark_tables, code))

    unlimited = without_limit(sql, "postgres")
    expected = _run(postgres, unlimited or sql)
    expected_count = None if unlimited is None else len(_run(postgres, sql).rows)
    expected, actual = comparable(expected, actual)
    problems = result_differences(expected, actual, order_keys, expected_count)

    context = [f"PostgreSQL query:\n{sql}", f"Generated code:\n{code}"]
    assert not problems, "\n\n".join([*context, *problems])
