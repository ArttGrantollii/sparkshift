# ADR 0002: PySpark is not a runtime dependency

- **Status:** Accepted
- **Date:** 2026-10-07

## Context

SparkShift **generates** PySpark source code; it never **executes** Spark.
PySpark is large (about 472 MB installed for 4.2.0) and requires a Java runtime.
It cannot run in Pyodide, which the planned browser playground depends on.

## Decision

- PySpark is a development dependency only (the `dev` dependency group), used
  by tests that execute generated code against real Spark.
- No module under `src/sparkshift` may import `pyspark` or `py4j`.
- Generated code is produced as text.
- The rule is enforced by `tests/test_architecture.py`, which imports every
  SparkShift module in a fresh interpreter and fails if either package was loaded.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Build PySpark `Column` objects, then render them to source | Requires PySpark at runtime, and `Column` objects cannot be reliably turned back into readable source code. |
| Optional runtime import to validate generated code | Adds a second code path and still needs Java; the equivalence test suite already validates generated code. |

## Consequences

**Positive**

- Small install with no Java requirement for users.
- The core can run in the browser.

**Negative**

- SparkShift cannot check generated code against PySpark at conversion time.
  Correctness depends on the equivalence test suite in CI, which must cover
  every construct marked as supported.
- Generated code targets a specific PySpark API. The tested PySpark version
  must be documented and kept current.
