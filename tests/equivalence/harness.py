"""Run a query both ways on Spark and assert the results are equivalent."""

from typing import TYPE_CHECKING

from compare import differences, snapshot

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
    spark: "SparkSession", sql: str, dialect: str | None = None
) -> None:
    """Assert that the generated PySpark returns the same result as spark.sql(sql).

    ``sql`` must also be valid Spark SQL, since it is executed directly as the
    reference result.
    """
    expected = snapshot(spark.sql(sql))
    code = sparkshift.convert(sql, dialect=dialect).code
    actual = snapshot(run_generated(spark, code))

    problems = differences(expected, actual)
    assert not problems, "\n\n".join(
        [f"SQL:\n{sql}", f"Generated code:\n{code}", *problems]
    )
