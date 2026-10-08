# Testing and correctness

SparkShift's central claim is that generated code returns the same result as
the original SQL. That claim is checked by executing both on real Apache Spark.

## Test layers

| Layer | Location | What it checks | Needs Spark |
|---|---|---|---|
| Unit | `tests/test_*.py` | Parsing, translation decisions, error messages | No |
| Golden output | `tests/test_emit.py`, `tests/test_api.py` | The exact generated code, so readability changes are deliberate | No |
| Architecture | `tests/test_architecture.py` | Dependency rules: no PySpark in the core, SQLGlot only in the frontend | No |
| Equivalence | `tests/equivalence/` | Original SQL and generated PySpark return equivalent results | Yes |

Run everything with `uv run pytest`, or skip Spark with `uv run pytest -m "not spark"`.

## Equivalence testing

```mermaid
flowchart LR
    SQL[SQL] --> Ref["spark.sql(sql)"]
    SQL --> Convert[sparkshift.convert] --> Exec[execute generated code]
    Ref --> Compare{compare}
    Exec --> Compare
```

For each scenario, `assert_equivalent(spark, sql)`:

1. Runs `spark.sql(sql)` to produce the reference result.
2. Converts the SQL with SparkShift and executes the generated code, which must
   assign a DataFrame to `result`.
3. Compares the two results using the rules below.

The SQL must also be valid Spark SQL, because it is executed directly as the
reference.

## Comparison rules

Comparing `collect()` output with `==` would be wrong for SQL. The comparator
in `tests/equivalence/compare.py` applies these rules:

| # | Rule | Why |
|---|---|---|
| 1 | **Row order is ignored** | Without `ORDER BY`, SQL guarantees no row order. Two correct plans may return rows in different orders. |
| 2 | **Rows are compared as a multiset** | Duplicate rows must match in number. Comparing as a set would hide a bug that removes duplicates. |
| 3 | **Column names, order, and types must match** | `SELECT *` must produce the same columns in the same order; `int` vs `bigint` or a different decimal scale is a real difference to consumers. |
| 4 | **Nullability flags are ignored** | Spark derives "may contain NULL" metadata differently depending on the code path. Actual NULL values are still compared. |
| 5 | **NULL matches NULL** | In SQL, `NULL = NULL` is not true, but when comparing results, NULL in both means they agree. |
| 6 | **Floats match within a tolerance; everything else exactly** | Floating-point addition is not associative, so a different evaluation order can change the last digits. Decimals, strings, dates, and timestamps must match exactly. |

Ordered comparison (for queries with `ORDER BY`) must also account for ties,
where rows with equal sort keys may appear in any order. It will be added
together with `ORDER BY` support.

### Testing the comparator

A comparator that always reports "equal" would make every equivalence test
pass and prove nothing. `tests/equivalence/test_compare.py` therefore includes
cases that must fail: a missing duplicate, NULL vs empty string, NULL vs zero,
`int` vs `bigint`, reordered columns, unequal decimals, and floats outside the
tolerance.

## Test data

`tests/equivalence/datasets.py` defines the `customers` and `orders` tables
with explicit schemas. Rows are chosen to exercise SQL semantics: exact
duplicate rows, NULLs in every nullable column, empty strings, zero and
negative amounts, non-ASCII text, a leap-day timestamp with microseconds,
orders with no or unknown customers, and a customer with no orders.

`tests/equivalence/test_datasets.py` fails if any of these edge cases is
removed, because weakening the data silently weakens every test that uses it.
