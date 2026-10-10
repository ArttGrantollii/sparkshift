# ADR 0005: SAS has its own front end over the shared IR

- **Status:** Accepted
- **Date:** 2026-10-10

## Context

Phase 4 adds SAS as a second input language. SAS is not a SQL dialect: a
program is a sequence of DATA and PROC steps, the DATA step processes rows one
at a time, and SAS's rules for missing values differ from SQL's NULL (a missing
number is smaller than every number, and a comparison is always true or false).

SQLGlot, which parses SQL for us (ADR 0001), has no SAS dialect. ADR 0003
introduced the IR so that another front end could reuse the emitter.

No SAS installation is available to test against: SAS is commercial software,
and CI cannot run it.

## Decision

- **A hand-written front end:** `sas_lexer.py` (tokens, comments, macro
  syntax), `sas_parser.py` (a small syntax tree for the subset SparkShift
  translates), and `sas_translate.py` (syntax tree to IR). No new dependency:
  the statement structure of SAS is simple enough to parse by hand.
- **The same IR and emitter as SQL.** Each DATA step becomes a named relation;
  `emit_datasets` declares every data set as a DataFrame variable and assigns
  the last one to `result`. The IR gained what SAS needs and SQL did not:
  renaming columns by name.
- **SAS semantics are written out in the generated PySpark**, for example
  `F.col("score").isNull() | (F.col("score") < F.lit(3))` for `score < 3`.
  The rules and their sources in SAS's public documentation are listed in
  [docs/sas.md](../sas.md).
- **The same front door:** `convert(program, dialect="sas")` and
  `sparkshift convert job.sas --dialect sas`. Errors use the existing types
  (`SASParseError` is an `SQLParseError`), and warnings are data, as before.
- **The same allowlist approach:** anything not explicitly supported is
  rejected, and every issue in a program is reported at once.
- **Clean room:** only SAS's public documentation is used.
- **Verification without SAS:** SAS scenarios run the generated PySpark on
  Spark and compare it with expected rows written by hand from the documented
  rules. The README says so.

## Consequences

- The SAS front end never imports SQLGlot or the SQL front end; the
  architecture tests enforce it.
- Correctness rests on reading SAS's documentation correctly. Each rule is
  cited, so a mistake can be found and fixed, and anyone with SAS can check a
  scenario against it.
- Some SAS behavior depends on information SparkShift does not have, such as
  whether a variable is numeric or text, or a data set's column order. Where it
  matters, SparkShift rejects the construct or warns, as it does for SQL.
- Later milestones add DATA step logic, PROC SORT, MEANS, FREQ, and SQL to the
  same front end.
