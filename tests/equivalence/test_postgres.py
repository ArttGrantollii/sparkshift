"""PostgreSQL scenarios on a real PostgreSQL server.

The dialect scenarios check SparkShift's PySpark against Spark SQL written by
hand to mean what the PostgreSQL query means. These tests remove that
assumption: they run the original query on PostgreSQL itself and compare its
rows with the generated PySpark's on Spark (see databases.py).

They need a server: set SPARKSHIFT_POSTGRES_URL to a connection URL for a
database the tests may write to. CI starts a throwaway PostgreSQL container
(see .github/workflows/ci.yml); without the variable these tests are skipped.
The tables are created in their own schema, which is dropped first.
"""

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest
from databases import (
    POSTGRES,
    TEST_SCHEMA,
    cases,
    check,
    configured_url,
    create_table_sql,
    insert_sql,
    run_query,
    skip_unless_configured,
)
from datasets import TABLES

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = [pytest.mark.spark, pytest.mark.database, skip_unless_configured(POSTGRES)]


@pytest.fixture(scope="module")
def postgres() -> Iterator[Any]:
    """A connection to PostgreSQL with the test tables loaded, in UTC."""
    import psycopg

    with psycopg.connect(configured_url(POSTGRES), autocommit=True) as connection:
        connection.execute("SET TIME ZONE 'UTC'")
        connection.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        connection.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        connection.execute(f"SET search_path TO {TEST_SCHEMA}")
        for name, (schema, rows) in TABLES.items():
            connection.execute(create_table_sql(POSTGRES, name, schema))
            with connection.cursor() as cursor:
                cursor.executemany(insert_sql(name, schema), rows)
        yield connection
        connection.execute(f"DROP SCHEMA {TEST_SCHEMA} CASCADE")


@pytest.mark.parametrize(("sql", "order_keys"), cases("postgres"))
def test_postgres_returns_what_the_pyspark_returns(
    postgres: Any,
    spark_tables: "SparkSession",
    sql: str,
    order_keys: list[str] | None,
) -> None:
    check(
        lambda query: run_query(postgres, query),
        spark_tables,
        sql,
        "postgres",
        order_keys,
    )
