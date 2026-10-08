# SparkShift

[![CI](https://github.com/ArttGrantollii/sparkshift/actions/workflows/ci.yml/badge.svg)](https://github.com/ArttGrantollii/sparkshift/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Convert SQL into readable, idiomatic, tested PySpark DataFrame code.

> **Status: early development.** SparkShift converts `SELECT` queries with
> joins, column expressions, `WHERE`, `DISTINCT`, and row limits (see below).
> Everything else is rejected with a clear error. See [ROADMAP.md](ROADMAP.md)
> for planned scope.

## Goal

Given a SQL query, SparkShift aims to generate PySpark DataFrame API code that a
data engineer would be comfortable maintaining — and to prove the generated code
is correct by executing both the original SQL and the generated PySpark against
the same data on real Apache Spark.

## Usage

```python
import sparkshift

result = sparkshift.convert(
    "SELECT order_id, amount * 1.10 AS with_tax FROM sales.orders"
)
print(result.code)
```

<!-- Exact SparkShift output, checked by tests/test_readme.py. Not reformatted. -->
<!-- fmt: off -->
```python
from decimal import Decimal

from pyspark.sql import functions as F

result = (
    spark.table("sales.orders")
    .select(
        F.col("order_id"),
        (F.col("amount") * F.lit(Decimal("1.10"))).alias("with_tax"),
    )
)
```
<!-- fmt: on -->

The generated code imports what it uses, expects a SparkSession named `spark`
(as in a Databricks notebook), and assigns the final DataFrame to `result`.

Pass `dialect=` for non-generic SQL: `tsql`, `postgres`, `mysql`, `snowflake`,
`bigquery`, or `oracle`.

Queries SparkShift cannot translate safely raise `UnsupportedSQLError`, listing
every unsupported construct:

```text
UnsupportedSQLError: 2 unsupported constructs:
  - WINDOW expression: ROW_NUMBER() OVER (ORDER BY name)
  - ORDER BY clause: ORDER BY name
```

## Supported SQL

| Construct | Status |
|---|---|
| `SELECT ... FROM table` (including `schema.table`, quoted names, and table aliases) | Supported |
| Columns, qualified columns (`t.column`), `*`, `t.*`, and `AS` aliases | Supported |
| Joins: `[INNER] JOIN`, `LEFT`/`RIGHT`/`FULL [OUTER] JOIN`, `CROSS JOIN`, comma joins | Supported, with `ON` or `USING` |
| Literals: integers, decimals, doubles (`1.5e0`), strings, `TRUE`/`FALSE`, `NULL` | Supported |
| Arithmetic: `+ - * / %` and unary `-` | Supported, with dialect exceptions below |
| Comparisons: `= <> != < <= > >=` | Supported |
| Logic: `AND OR NOT` (SQL three-valued logic with NULL) | Supported |
| `WHERE` | Supported |
| `DISTINCT` | Supported (`DISTINCT ON` is not) |
| Row limits: `LIMIT n`, T-SQL `TOP n`, `FETCH FIRST n ROWS ONLY` | Supported, for a constant `n` |
| Aggregates: `COUNT(*)`, `COUNT`, `COUNT(DISTINCT ...)`, `SUM`, `SUM(DISTINCT ...)`, `AVG`, `MIN`, `MAX` | Supported |
| `GROUP BY` columns, positions (`GROUP BY 1`), and `HAVING` | Supported, with dialect exceptions below |
| `IN (...)`, `BETWEEN`, `LIKE`, `ILIKE`, `IS [NOT] NULL`, `IS [NOT] DISTINCT FROM`, `<=>` | Supported (constant `LIKE` patterns) |
| `CASE` (searched and simple), `IF`, `IIF`, `COALESCE`, `IFNULL`, `NVL`, `NULLIF` | Supported |
| `CAST`, `TRY_CAST`, `::` to integer types, `DECIMAL(p, s)`, `DOUBLE`, `VARCHAR`/`TEXT`/`STRING`, `DATE`, `BOOLEAN` | Supported |
| String functions: `UPPER`, `LOWER`, `LENGTH`/`LEN`/`CHAR_LENGTH`, `TRIM`/`LTRIM`/`RTRIM`, `SUBSTRING`/`SUBSTR`, `CONCAT`, `\|\|`, `REPLACE`, `LEFT`, `RIGHT` | Supported, with dialect rules below |
| Numeric functions: `ABS`, `ROUND`, `CEIL`/`CEILING`, `FLOOR`, `POWER`, `SQRT`, `SIGN`, `LN`, `LOG(base, x)`, `EXP`, `GREATEST`, `LEAST` | Supported, with dialect rules below |
| `NATURAL`, semi, anti, and as-of joins; joins to subqueries; `LATERAL` and `APPLY` | Unsupported — rejected with an error |
| `GROUP BY` expressions, `ROLLUP`, `CUBE`, `GROUPING SETS`, `AVG(DISTINCT ...)` | Unsupported — rejected with an error |
| `IN (subquery)`, `LIKE ... ESCAPE`, `IS TRUE`/`IS FALSE`, casts to `FLOAT`/`REAL`, `CHAR(n)`/`VARCHAR(n)`, unparameterized `DECIMAL`, and timestamps | Unsupported — rejected with an error |
| Everything else, including `ORDER BY`, window functions, date functions, and other functions | Unsupported — rejected with an error |

Support grows feature by feature; see the [roadmap](ROADMAP.md).

### Dialect emulations

Where a source dialect's function behaves differently from Spark's but an exact
PySpark equivalent exists, SparkShift generates code with the source dialect's
behavior:

| Construct | Dialect | Behavior | Generated as |
|---|---|---|---|
| `LEN(x)` | T-SQL | Ignores trailing spaces | `F.length(F.rtrim(x))` |
| `LENGTH(x)` | MySQL | Counts bytes, not characters | `F.octet_length(x)` |
| `CONCAT(...)` | PostgreSQL, T-SQL, Oracle | Skips NULL inputs | `F.concat_ws("", ...)` |
| `a \|\| b` | Oracle | Treats NULL as an empty string | `F.concat_ws("", a, b)` |
| `a \|\| b` | MySQL | Logical OR, not concatenation | `a \| b` |

### Dialect differences

SparkShift generates code with Spark semantics. Where a construct in the
source dialect means something different in Spark, has no Spark equivalent, or
could mean two things that only the table schema would distinguish, SparkShift
rejects it rather than guess:

| Construct | Dialects | Why it is rejected |
|---|---|---|
| `/` | T-SQL, PostgreSQL | Dividing two integers discards the remainder; Spark returns a fraction. |
| `/` | MySQL | Division by zero returns NULL; Spark raises an error. |
| `+` | T-SQL | `+` also concatenates strings. |
| `WHERE` referring to a `SELECT` alias | Snowflake | Spark does not allow it, and the name could also be a real column. |
| `ROWNUM`, `ROWID` | Oracle | Pseudo-columns with no Spark equivalent; use `FETCH FIRST n ROWS ONLY`. |
| `(+)` outer-join marker | Oracle | Ignoring it would turn an outer join into an inner join; use `LEFT`/`RIGHT JOIN`. |
| `TOP n PERCENT`, `WITH TIES`, `OFFSET` | T-SQL, Oracle, others | Not equivalent to a plain row limit. |
| `GROUP BY 1` | Oracle, T-SQL | Oracle groups by the constant 1; T-SQL does not allow positions. |
| `GROUP BY` or `HAVING` naming a `SELECT` alias | All | Databases differ on whether a same-named column wins, and only the schema would tell. |
| Selecting a column that is neither grouped nor aggregated | MySQL (relaxed mode) | MySQL returns an arbitrary value; Spark raises an error. |
| `ISNULL(a, b)` | T-SQL | Returns the first argument's type (`ISNULL(int_col, 1.5)` is 1); `COALESCE` returns 1.5. |
| `CAST(x AS VARCHAR)` without a length | T-SQL | Means `VARCHAR(30)` and truncates longer values. |
| `CAST(x AS FLOAT)` / `REAL` | All | Sizes differ: T-SQL `FLOAT` is 8 bytes, Spark `FLOAT` is 4. |
| `LIKE '[a-c]%'` | T-SQL | `[ ]` is a character class in T-SQL; Spark matches it literally. |
| `LIKE` patterns containing `\` | All | Whether backslash escapes wildcards differs between databases and Spark. |
| `ROUND(x)` | PostgreSQL, MySQL, Oracle | Floating-point halves round to even (`2.5` → `2`) but exact numbers away from zero; Spark always rounds away, and only the column type would tell. |
| `ROUND(x, n, 1)` | T-SQL | The third argument truncates instead of rounding. |
| `GREATEST`, `LEAST` | MySQL, Oracle, Snowflake, BigQuery | Return NULL when any argument is NULL; Spark ignores NULL arguments. |
| `LOG(x)` | PostgreSQL, Snowflake, Oracle, generic | Base 10 in some databases, natural logarithm in others; write `LN(x)` or `LOG(base, x)`. |
| `TRIM` | BigQuery | Removes all Unicode whitespace, including tabs; Spark removes only spaces. |
| `SUBSTRING`, `LEFT`, `RIGHT` with negative or computed positions | All | Negative positions behave differently across databases. |

### Known limitations

- Database-specific string collation is not emulated. For example, MySQL and
  SQL Server often compare strings, including in `LIKE` and `REPLACE`,
  case-insensitively; Spark compares them case-sensitively.
- Oracle treats an empty string as NULL; SparkShift does not emulate this.
- An unaliased `SELECT` item that calls a function, such as `SELECT UPPER(name)`,
  must be given an alias, because Spark names the column after the exact
  spelling used (`CEIL` or `CEILING`), which is lost in parsing.
- `CAST` failures follow Spark's ANSI behavior: an invalid value raises an
  error (use `TRY_CAST` for NULL instead), where some databases, such as MySQL,
  return a default value.
- An unaliased negation, `BETWEEN`, or `IF` in the `SELECT` list must be given
  an alias, because Spark SQL and PySpark name those output columns
  differently.
- Long expressions are not wrapped across lines yet.
- A `JOIN` without `ON` or `USING` (accepted by MySQL) is translated as a cross
  join, because SQLGlot represents it exactly like the comma form `FROM a, b`.
- When an aggregate query's `SELECT` order differs from the `GROUP BY` order,
  or a key is not selected, unaliased aggregates must be given an alias.

## How correctness is verified

Supported translations are backed by automated equivalence tests that execute
both the original SQL and the generated PySpark against the same datasets on
real Apache Spark, then compare the results with SQL semantics in mind: row
order is ignored unless the query orders it, duplicate rows must match in
number, column names and types must match, and NULLs are compared as values.
The test data deliberately includes duplicates, NULLs, empty strings, and
other edge cases. See [docs/testing.md](docs/testing.md).

## Development setup

SparkShift is developed on Linux (WSL2 on Windows works). You need:

- [uv](https://docs.astral.sh/uv/) — manages Python and project dependencies
- Git
- Java 17, 21, or 25 (development and CI use 21) — only for running the Spark tests

```bash
git clone https://github.com/ArttGrantollii/sparkshift.git
cd sparkshift
uv sync
uv run pre-commit install
```

`uv sync` installs the Python version pinned in `.python-version` if needed,
creates a `.venv`, and installs SparkShift in editable mode together with the
development tools. PySpark is a development dependency only — SparkShift
generates PySpark code but does not need Spark to run.

### Running checks

```bash
uv run pytest                  # all tests, including real Spark
uv run pytest -m "not spark"   # fast tests only; does not start Spark
uv run pytest --cov            # with coverage report
uv run ruff check .            # lint
uv run ruff format --check .   # formatting
```

## Design documents

- [Architecture](docs/architecture.md) — how the core is structured and the rules that keep it portable
- [Testing and correctness](docs/testing.md) — test layers, equivalence testing, and result-comparison rules
- [Architecture Decision Records](docs/adr/) — significant decisions and their trade-offs

## License

[Apache License 2.0](LICENSE)
