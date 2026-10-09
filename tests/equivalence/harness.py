"""Run a query both ways on Spark and assert the results are equivalent."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

import sqlglot
from compare import differences, limited_differences, ordered_differences, snapshot
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
    order_keys: Sequence[str] | None = None,
) -> None:
    """Assert that the generated PySpark returns the same result as the SQL.

    The reference result comes from running ``reference_sql`` (default:
    ``sql`` itself) with spark.sql, so it must be valid Spark SQL. Pass it
    explicitly when ``sql`` is in a dialect Spark cannot run, such as T-SQL.

    A query with ORDER BY needs ``order_keys``: the output columns whose
    values decide the order. Rows equal on all of them may come in any order.
    When the query sorts by a column it does not output, choose data with no
    ties on that column and pass every output column.
    """
    reference = reference_sql or sql
    ordered = has_order_by(reference)
    if ordered != (order_keys is not None):
        raise ValueError("Pass order_keys exactly when the query has ORDER BY.")
    code = sparkshift.convert(sql, dialect=dialect).code
    actual = snapshot(run_generated(spark, code))

    unlimited = without_limit(reference)
    if unlimited is None:
        expected = snapshot(spark.sql(reference))
        expected_count = None
    else:
        expected = snapshot(spark.sql(unlimited))
        expected_count = spark.sql(reference).count()
    if order_keys is not None:
        problems = ordered_differences(expected, actual, order_keys, expected_count)
    elif expected_count is not None:
        problems = limited_differences(expected, actual, expected_count)
    else:
        problems = differences(expected, actual)

    context = [f"SQL:\n{sql}"]
    if reference != sql:
        context.append(f"Reference SQL:\n{reference}")
    context.append(f"Generated code:\n{code}")
    assert not problems, "\n\n".join([*context, *problems])


def has_order_by(spark_sql: str) -> bool:
    tree = sqlglot.parse_one(spark_sql, read="spark")
    return isinstance(tree, exp.Select) and tree.args.get("order") is not None


def without_limit(spark_sql: str) -> str | None:
    """Return the query without its LIMIT, or None if it has none.

    A LIMIT may cut a group of tied rows, or without ORDER BY keep any rows,
    so limited results are checked against the unlimited result (see
    ``limited_differences`` and ``ordered_differences``).
    """
    tree = sqlglot.parse_one(spark_sql, read="spark")
    if not isinstance(tree, exp.Select) or tree.args.get("limit") is None:
        return None
    unlimited = tree.copy()
    unlimited.set("limit", None)
    return unlimited.sql(dialect="spark")
