# SAS support

SparkShift converts a subset of SAS into PySpark. This page lists what is
supported, the SAS rules the generated code follows, and where each rule is
documented. SAS support is **in progress** (see the [roadmap](../ROADMAP.md)).

## How SAS support is verified

SAS is commercial software, and no SAS installation is available to test
against. Instead, SAS scenarios (`tests/equivalence/test_sas.py`) run the
generated PySpark on Apache Spark and compare its result with **expected rows
written by hand** from the rules below. Each scenario names the rule it
checks. This verifies SparkShift against SAS's documented behavior, not
against SAS itself.

All rules come from SAS's public documentation: *SAS 9.4 Language Reference:
Concepts*, at documentation.sas.com.

## Supported so far

| Construct | Notes |
|---|---|
| `DATA out; SET in; RUN;` | One output data set and one input data set per step. A step also ends at the next `DATA` or `PROC` statement, as in SAS |
| Several DATA steps | Each data set becomes a DataFrame variable; later steps read earlier data sets. `result` is the last one |
| `lib.member` | Permanent data sets are read with `spark.table("lib.member")`. A temporary (`WORK`) data set the program did not create is read the same way, by its name |
| `KEEP=`, `DROP=`, `RENAME=`, `WHERE=` on the input data set | |
| `KEEP=`, `DROP=`, `RENAME=` on the output data set | |
| `WHERE`, `KEEP`, `DROP`, `RENAME` statements | |
| Conditions | Comparisons (`=`, `^=`, `<`, `<=`, `>`, `>=` and `EQ`, `NE`, `LT`, `LE`, `GT`, `GE`) of a variable with a constant; `AND`, `OR`, `NOT (...)`; `IN`, `NOT IN`; `BETWEEN ... AND`; `IS [NOT] MISSING` and `IS [NOT] NULL` |
| Comments | `/* ... */` anywhere, and `* ... ;` statements |

Everything else is rejected with a list of every unsupported construct, for
example assignments, `IF`, `RETAIN`, `BY`, `MERGE`, arrays, functions,
arithmetic in conditions, date constants, variable lists such as `x1-x5`,
macros (`%let`, `&name`), global statements such as `LIBNAME` and `OPTIONS`,
`DATA _NULL_`, writing to a permanent library, and every PROC step.

## The rules

### Order of dropping, keeping, and renaming

*Language Reference: Concepts, "Dropping, Keeping, and Renaming Variables",
Order of Application:*

1. Options on input data sets are evaluated left to right; `DROP=` and `KEEP=`
   are applied before `RENAME=`. Other input data set options use the **old**
   names; program statements use the new ones.
2. Next, the `DROP` and `KEEP` statements are applied, followed by the `RENAME`
   statement. Program statements use the old names; output data set options
   use the new ones.
3. Finally, options on output data sets are evaluated left to right; `DROP=`
   and `KEEP=` are applied before `RENAME=`.

SparkShift applies an input `WHERE=` first, with the old names, then `KEEP=`
and `DROP=`, then `RENAME=`; then the `WHERE` statement; then the statements;
then the output options.

### WHERE= and the WHERE statement

*Data Set Options, "WHERE=":* if a DATA step has both the `WHERE=` option and
the `WHERE` statement, SAS ignores the `WHERE` statement for data sets with
the `WHERE=` option. SparkShift does the same and warns that the statement is
ignored.

### Missing numbers

*Language Reference: Concepts, "SAS Operators in Expressions":*

- "A missing numeric value is smaller than any other numeric value." Missing
  values are the lowest in any comparison.
- A comparison is 1 (true) or 0 (false), never missing, and `NOT` of a missing
  value is true.

So in SAS, `score < 3` is true for a missing score, where in SQL and Spark it
is NULL and the row is dropped. SparkShift writes the rule out:

| SAS | PySpark |
|---|---|
| `x = 3` | `F.col("x").eqNullSafe(F.lit(3))` |
| `x ^= 3` | `~F.col("x").eqNullSafe(F.lit(3))` |
| `x < 3`, `x <= 3` | `F.col("x").isNull() \| (F.col("x") < F.lit(3))` |
| `x > 3`, `x >= 3` | `F.col("x").isNotNull() & (F.col("x") > F.lit(3))` |
| `x = .` | `F.col("x").isNull()` |
| `x in (1, 2)` | `F.col("x").isNotNull() & F.col("x").isin(...)` |
| `x in (1, .)` | `F.col("x").isNull() \| F.col("x").isin(...)` |
| `x between 1 and 5` | `F.col("x").isNotNull() & F.col("x").between(...)` |

Every condition is true or false, never NULL, so `NOT` works as in SAS.

### Text

*Language Reference: Concepts, "SAS Operators in Expressions", Character
Comparisons:*

- Values of unequal length are compared as if blanks were added to the end of
  the shorter one, so trailing blanks do not count: `'fox '` equals `'fox'`.
  Leading blanks count.
- A blank, or missing text value, is smaller than any other printable text.

A NULL text value in a Spark table is SAS's missing (blank) text. SparkShift
compares text as `F.coalesce(F.rtrim(F.col("name")), F.lit(""))`, with the
constant's trailing blanks removed.

### IS MISSING

A missing number is NULL; missing text is NULL or blank. SparkShift cannot see
a variable's type, so `x is missing` becomes
`F.col("x").isNull() | (F.rtrim(F.col("x")) == F.lit(""))`, which is right for
both: a number is never blank.

## Limitations

- **Column order of KEEP:** SAS keeps variables in the data set's order;
  SparkShift cannot see that order and keeps them in the listed order. It warns
  whenever a `KEEP` names several variables.
- **Variable types:** SparkShift cannot see whether a variable is a number or
  text, which SAS compares differently. A comparison needs a constant on one
  side to tell; comparing two variables is rejected.
- **Text order:** ordering comparisons on text (`<`, `>`) ignore trailing
  blanks. They differ from SAS only for text that ends in characters that sort
  below a blank, such as tabs.
- **Values pass through unchanged:** a NULL text value stays NULL in the
  result, where SAS would show a blank.
- **NOT needs parentheses:** in SAS, `NOT` binds more tightly than a comparison
  (`not x = 1` means `(not x) = 1`). SparkShift requires `NOT (...)`, so the
  meaning is never in doubt.
