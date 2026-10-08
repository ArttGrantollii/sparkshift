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
