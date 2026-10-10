"""MySQL scenarios on a real MySQL server, as test_postgres.py does for
PostgreSQL (see databases.py for how results are compared).

They need a server: set SPARKSHIFT_MYSQL_URL to a URL such as
mysql://root@127.0.0.1:3306 for a server the tests may write to. CI starts a
throwaway MySQL container (see .github/workflows/ci.yml); without the
variable these tests are skipped. The tables are created in their own
database, which is dropped first.

The test database compares strings exactly, by code point (utf8mb4_0900_bin),
as Spark does. MySQL's default collation ignores case and accents; SparkShift
does not emulate that (see the README's known limitations), so these tests
check what SparkShift claims to convert exactly.
"""

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

import pytest
from databases import (
    MYSQL,
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

pytestmark = [pytest.mark.spark, pytest.mark.database, skip_unless_configured(MYSQL)]


@pytest.fixture(scope="module")
def mysql() -> Iterator[Any]:
    """A connection to MySQL with the test tables loaded, in UTC."""
    import pymysql

    url = urlsplit(configured_url(MYSQL))
    connection = pymysql.connect(
        host=url.hostname or "127.0.0.1",
        port=url.port or 3306,
        user=unquote(url.username or "root"),
        password=unquote(url.password or ""),
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute(f"DROP DATABASE IF EXISTS {TEST_SCHEMA}")
            cursor.execute(
                f"CREATE DATABASE {TEST_SCHEMA} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
            )
            cursor.execute(f"USE {TEST_SCHEMA}")
            for name, (schema, rows) in TABLES.items():
                cursor.execute(create_table_sql(MYSQL, name, schema))
                cursor.executemany(insert_sql(name, schema), rows)
        yield connection
        with connection.cursor() as cursor:
            cursor.execute(f"DROP DATABASE {TEST_SCHEMA}")
    finally:
        connection.close()


@pytest.mark.parametrize(("sql", "order_keys"), cases("mysql"))
def test_mysql_returns_what_the_pyspark_returns(
    mysql: Any,
    spark_tables: "SparkSession",
    sql: str,
    order_keys: list[str] | None,
) -> None:
    check(lambda query: run_query(mysql, query), spark_tables, sql, "mysql", order_keys)
