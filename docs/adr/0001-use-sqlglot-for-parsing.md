# ADR 0001: Use SQLGlot for SQL parsing

- **Status:** Accepted
- **Date:** 2026-10-07

## Context

SparkShift must turn SQL text into a tree it can analyze. Input SQL may come
from several dialects (ANSI, T-SQL, PostgreSQL, MySQL, Snowflake, BigQuery,
Oracle). Writing and maintaining a correct parser for even one dialect is a
large project on its own, and it is not the problem SparkShift exists to solve.

The parser must also run inside a browser via Pyodide, which rules out anything
that needs a JVM or native code without a WebAssembly build.

## Decision

Use [SQLGlot](https://github.com/tobymao/sqlglot) **only as a parser**.
SparkShift walks SQLGlot's syntax tree and makes its own translation decisions.
We do not use SQLGlot's SQL-to-SQL transpilation as the product.

Install plain `sqlglot`, without the optional compiled extras (`[c]`, `[rs]`),
so the dependency stays pure Python.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Hand-written parser | Full control, but enormous scope across seven dialects; would dominate the project. |
| ANTLR grammars | Requires Java tooling to generate parsers, one grammar per dialect, and generated code that is hard to read. |
| `sqlparse` | A tokenizer and formatter; it does not build a real syntax tree. |
| SQLGlot transpile to Spark SQL, then `spark.sql(...)` | Produces SQL strings, not DataFrame API code — the opposite of the project's goal. |
| Spark's own SQL parser | Requires a JVM, which breaks the browser playground and forces Spark on every user. |

## Consequences

**Positive**

- Multi-dialect parsing from one library.
- Pure Python, zero runtime dependencies, MIT-licensed — works in Pyodide.

**Negative**

- SparkShift is coupled to SQLGlot's syntax-tree classes, which change between
  major versions and release frequently. Mitigations: keep SQLGlot-specific
  code inside the analysis layer, use a bounded version range, and rely on the
  test suite to detect behavior changes on upgrade.
- SQLGlot may accept SQL more leniently than the source database does. Parsing
  successfully does not mean a construct is supported; SparkShift's own
  analysis decides that.
