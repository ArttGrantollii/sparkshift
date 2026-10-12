# ADR 0006: PROC SQL is translated by the SAS front end, not the SQL one

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

PROC SQL looks like the SQL that SparkShift already converts, so it would be
natural to pass its queries to the SQL front end (SQLGlot, ADR 0001). But
"PROC SQL follows the SAS convention for handling missing values" (*SAS SQL
Procedure User's Guide*): a missing value is smaller than any other value, and
a comparison is always true or false. In PROC SQL, `where score < 3` keeps
rows whose score is missing; in SQL, and in SparkShift's SQL conversion, it
drops them. A join condition `a.id = b.id` matches missing values with each
other; in SQL it does not.

PROC SQL also has SAS syntax that SQL parsers read differently or not at all:
`"text"` is a string, not a quoted identifier; `EQ`, `NE`, and `IS MISSING`;
data set options such as `(keep=a b)` after a table name; and SAS functions,
where `SUM(a, b)` adds two values of a row instead of summarizing rows.

## Decision

- **PROC SQL is parsed and translated by the SAS front end.** The SAS parser
  reads `CREATE TABLE ... AS SELECT` statements, reusing its expression
  parser, so every condition and value means what it means in SAS.
- **Conditions use the DATA step's rules** (ADR 0005, docs/sas.md): missing
  values are the smallest, comparisons are never NULL, text ignores trailing
  blanks.
- **An equality of two columns is translated with a warning.** SparkShift
  cannot see whether the columns are numbers or text. `eqNullSafe` is SAS's
  comparison for numbers, missing included; for text, SAS would also ignore
  trailing blanks and treat empty text as missing. Joins are the main use of
  such equalities, so rejecting them would make PROC SQL joins unusable;
  converting them silently would hide a possible difference. Other
  comparisons of two columns stay rejected, as in a DATA step.
- **A supported subset:** single `SELECT` queries with `INNER` and `LEFT`
  joins, `WHERE`, `GROUP BY`, `HAVING`, `ORDER BY`, `DISTINCT`, and summary
  functions. Subqueries, set operators, `CALCULATED`, remerging, and other
  statements are rejected, every issue reported at once.

## Consequences

- PROC SQL and the DATA step share one implementation of SAS's rules, and
  the SAS front end still never imports SQLGlot (architecture tests).
- The SQL translator's features, such as subqueries and set operations, are
  not available to PROC SQL until the SAS front end implements them with SAS's
  rules.
- A query that a user expects to behave like ANSI SQL behaves like SAS
  instead, which is what the SAS program did.
