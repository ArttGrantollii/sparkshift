"""Run a query both ways on Spark and assert the results are equivalent."""

from typing import TYPE_CHECKING

import sqlglot
from compare import differences, limited_differences, snapshot
from sqlglot import exp

import sparkshift

if TYPE_CHECKING:
    from pyspark.sql import DataFrame, SparkSession


def run_generated(spark: "SparkSession", code: str) -> "DataFrame":
    """Execute generated code and return the DataFrame it assigns to ``result``.

    exec() is acceptable here because the code is SparkShift's own output,
    running inside the test suite. User-facing interfaces never execute code.
    """
    from pyspark.sql import DataFrame

    namespace: dict[str, object] = {"spark": spark}
    exec(compile(code, "<sparkshift-generated>", "exec"), namespace)
    result = namespace.get("result")
    if not isinstance(result, DataFrame):
        actual_type = type(result).__name__
        raise AssertionError(
            f"Generated code must assign a DataFrame to `result`, got {actual_type}"
        )
    return result


def assert_equivalent(
    spark: "SparkSession",
    sql: str,
    dialect: str | None = None,
    reference_sql: str | None = None,
) -> None:
    """Assert that the generated PySpark returns the same result as the SQL.

    The reference result comes from running ``reference_sql`` (default:
    ``sql`` itself) with spark.sql, so it must be valid Spark SQL. Pass it
    explicitly when ``sql`` is in a dialect Spark cannot run, such as T-SQL.
    """
    reference = reference_sql or sql
    code = sparkshift.convert(sql, dialect=dialect).code
    actual = snapshot(run_generated(spark, code))

    unlimited = without_limit(reference)
    if unlimited is None:
        problems = differences(snapshot(spark.sql(reference)), actual)
    else:
        expected_count = spark.sql(reference).count()
        problems = limited_differences(
            snapshot(spark.sql(unlimited)), actual, expected_count
        )

    context = [f"SQL:\n{sql}"]
    if reference != sql:
        context.append(f"Reference SQL:\n{reference}")
    context.append(f"Generated code:\n{code}")
    assert not problems, "\n\n".join([*context, *problems])


def without_limit(spark_sql: str) -> str | None:
    """Return the query without its LIMIT if it limits rows without ORDER BY.

    Such a query may return any rows, so it is checked against the unlimited
    result instead (see ``limited_differences``). Returns None otherwise.
    """
    tree = sqlglot.parse_one(spark_sql, read="spark")
    if not isinstance(tree, exp.Select) or tree.args.get("order"):
        return None
    if tree.args.get("limit") is None:
        return None
    unlimited = tree.copy()
    unlimited.set("limit", None)
    return unlimited.sql(dialect="spark")
