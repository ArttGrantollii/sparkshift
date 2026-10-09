# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project skeleton: `src/` layout, `pyproject.toml` packaging, Apache-2.0 license.
- Test and quality tooling: pytest, coverage, Ruff, and pre-commit hooks.
- Architecture test ensuring the core package never imports PySpark.
- Spark smoke test verifying a local SparkSession can execute SQL.
- GitHub Actions CI: lint, and tests on Python 3.11 and 3.14 with Java 21.
- Architecture note and first ADRs (SQLGlot for parsing; PySpark not a runtime dependency).
- SQL parsing for the generic dialect plus T-SQL, PostgreSQL, MySQL, Snowflake,
  BigQuery, and Oracle, with SparkShift error types (`SQLParseError`,
  `UnsupportedDialectError`, `MultipleStatementsError`) that report readable
  messages with line and column.
- `sparkshift.convert()`: the first public API. Converts `SELECT * FROM table`
  into PySpark and returns a `ConversionResult`; all other queries raise
  `UnsupportedSQLError` listing every unsupported construct.
- Intermediate representation and emitter (ADR 0003), with architecture tests
  keeping SQLGlot out of both.
- Spark equivalence testing: executes the original SQL and the generated
  PySpark on the same datasets and compares results with SQL semantics
  (unordered multiset rows, exact schema, NULL-aware, float tolerance).
  Test datasets cover duplicates, NULLs, empty strings, and other edge cases.
- Expressions in the SELECT list: columns, qualified columns, `*`, aliases,
  literals (integer, exact decimal, double, string, boolean, NULL), arithmetic,
  comparisons, and `AND`/`OR`/`NOT`. Generated code imports what it uses and
  adds parentheses according to Python's operator precedence.
- Operators whose meaning differs from Spark in the source dialect (integer
  division in T-SQL/PostgreSQL, NULL-on-zero division in MySQL, `+` in T-SQL)
  are rejected with an explanation.
- `WHERE`, `DISTINCT`, and row limits (`LIMIT n`, T-SQL `TOP n`,
  `FETCH FIRST n ROWS ONLY`), emitted in SQL's logical evaluation order.
  `DISTINCT ON`, `PERCENT`, `WITH TIES`, `OFFSET`, Oracle `ROWNUM`/`ROWID`, and
  Snowflake `WHERE` references to `SELECT` aliases are rejected.
- Joins: inner, left, right, full, and cross joins (including comma joins),
  with `ON` conditions or `USING` columns; table aliases, self-joins, chained
  joins, and `t.*`. Generated code declares each source table once and aliases
  joined tables so qualified columns resolve. Natural, semi, anti, and as-of
  joins, joins to subqueries, `LATERAL`/`APPLY`, and Oracle's `(+)` marker are
  rejected.
- Aggregation: `GROUP BY` columns and positions, `HAVING`, and `COUNT(*)`,
  `COUNT`, `COUNT(DISTINCT)`, `SUM`, `SUM(DISTINCT)`, `AVG`, `MIN`, `MAX`,
  including aggregate expressions. `HAVING` on aggregates not in the `SELECT`
  list uses helper columns that a final projection drops. Rejects columns that
  are neither grouped nor aggregated, ambiguous alias references, Oracle and
  T-SQL `GROUP BY` positions, and `ROLLUP`/`CUBE`/`GROUPING SETS`.
- Predicates and conditionals: `IN` lists, `BETWEEN`, `LIKE`/`ILIKE` with
  constant patterns, `IS [NOT] NULL`, NULL-safe equality, searched and simple
  `CASE`, `IF`/`IIF`, `COALESCE`/`IFNULL`/`NVL`, `NULLIF`, and
  `CAST`/`TRY_CAST`/`::` to an allowlist of types with the same meaning in
  every supported dialect. Rejects T-SQL `ISNULL`, T-SQL `VARCHAR` casts
  without a length, `FLOAT`/`REAL` casts, T-SQL `LIKE` character classes, and
  backslashes in `LIKE` patterns.
- String and numeric functions: `UPPER`, `LOWER`, `LENGTH`, `TRIM` variants,
  `SUBSTRING`, `CONCAT`, `||`, `REPLACE`, `LEFT`, `RIGHT`, `ABS`, `ROUND`,
  `CEIL`, `FLOOR`, `POWER`, `SQRT`, `SIGN`, `LN`, `LOG(base, x)`, `EXP`,
  `GREATEST`, `LEAST`. Emulates T-SQL `LEN`, MySQL byte-counting `LENGTH`, and
  NULL-skipping `CONCAT` exactly; rejects `ROUND` where floating-point halves
  round to even, NULL-propagating `GREATEST`/`LEAST`, ambiguous one-argument
  `LOG`, BigQuery `TRIM`, and non-constant or negative positions.
- Dates and timestamps: date parts, `CURRENT_DATE`/`CURRENT_TIMESTAMP`, day
  differences with each dialect's argument order, type-preserving date
  arithmetic in days, weeks, months, and years via `make_interval`, Spark's
  `DATE_ADD`/`DATE_SUB`, PostgreSQL and BigQuery `DATE_TRUNC`, and casts that
  map each dialect's timestamp types to Spark's `timestamp` (point in time) or
  `timestamp_ntz` (wall clock). Rejects seconds, weekdays, week numbers,
  non-day `DATEDIFF` units, input-type-preserving `DATE_TRUNC`, end-of-month
  `ADD_MONTHS`, T-SQL `DATETIME`/`SMALLDATETIME`/`TIMESTAMP`, and `SYSDATE`.
- Unaliased `SELECT` items that call functions now require an alias, because
  Spark names such columns after the original spelling.
- A `products` test table with whitespace, multi-byte text, and rounding
  boundaries.
- Generic IR child traversal, so checks such as "column is neither grouped nor
  aggregated" see inside every expression type.
- Equivalence testing for `LIMIT` without `ORDER BY` (row count plus
  sub-multiset of the unlimited result), and dialect scenarios checked against
  hand-written Spark SQL references.
- Long expressions are wrapped to fit 88 columns: one condition per line
  for `&`/`|` chains, one branch per line for `F.when`, one argument per
  line for long calls, and Black-style parentheses before `.alias`.
  Tests check that every equivalence scenario's code fits, and that
  wrapping never changes the Python syntax tree.
- `ORDER BY` on columns, aliases, positions, expressions, and aggregates, with
  `ASC`/`DESC` and `NULLS FIRST`/`NULLS LAST`. NULLs are placed as the
  source dialect does (PostgreSQL, Oracle, and Snowflake put them last when
  ascending). Keys that are not selected sort before the projection, or in
  aggregate queries through helper columns. Rejects aliases inside `ORDER BY`
  expressions, constants, ambiguous names, and keys outside a `SELECT
  DISTINCT` list.
- Ordered equivalence testing: rows that tie on the sort keys may come in any
  order, and a `LIMIT` may cut the last group of ties.
- Window functions: `ROW_NUMBER`, `RANK`, `DENSE_RANK`, and `COUNT`, `SUM`,
  `AVG`, `MIN`, `MAX` over `PARTITION BY` and `ORDER BY`, with SQL's default
  frame and each dialect's NULL placement. Each distinct window is declared
  once as a variable. Rejects explicit frames, other window functions,
  `IGNORE NULLS`, named windows, `DISTINCT` in windows, and windows outside
  the `SELECT` list or in aggregate queries.
- `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`, and `NTILE`, and explicit `ROWS`
  and `RANGE` window frames. `IGNORE NULLS` for `FIRST_VALUE` and
  `LAST_VALUE`. Snowflake's documented whole-window default for `FIRST_VALUE`
  and `LAST_VALUE` is written out as an explicit frame; BigQuery, whose
  default is undocumented, needs an explicit frame. Rejects `RANGE` offsets,
  `GROUPS` frames, `EXCLUDE`, and frames on ranking and offset functions.

### Fixed

- `ROW_NUMBER`, `RANK`, and `DENSE_RANK` without `ORDER BY` in the window are
  now rejected; they produced code that Spark refuses to run.
