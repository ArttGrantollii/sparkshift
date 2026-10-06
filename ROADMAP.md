# Roadmap

SparkShift is in early development. This roadmap describes intended scope,
not current capabilities. See the README for what works today.

Status legend: **Done** · **In progress** · **Planned**

| Phase | Scope | Status |
|---|---|---|
| 0 | Engineering foundation: packaging, tooling, CI | In progress |
| 1 | Core SQL → PySpark engine: expressions, SELECT/WHERE, joins, aggregation, ordering, window functions, CTEs, set operations — with Spark equivalence tests | Planned |
| 2 | CLI (`sparkshift convert`), packaging polish, verified examples | Planned |
| 3 | Advanced SQL: subqueries (IN / EXISTS), QUALIFY, multi-dialect testing, coverage reporting | Planned |
| 4 | SAS → PySpark for a documented subset (DATA step, PROC SQL/SORT/MEANS/FREQ) | Planned |
| 5 | Databricks notebook generation and deployment | Planned |
| 6 | Public browser-based playground (live demo) | Planned |
| 7 | Documentation polish and 1.0 release on PyPI | Planned |

## Guiding principle

A smaller set of constructs that are verified correct is preferred over a large
set that is not. Constructs SparkShift cannot translate safely are rejected with
a clear error instead of producing code that might be wrong.
