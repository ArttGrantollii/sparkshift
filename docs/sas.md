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
| Assignments | `net = amount * (1 - discount);`, written as `.withColumn(...)` in order |
| Arithmetic | `+`, `-`, `*`, `/`, and a leading minus |
| `IF ... THEN ...; ELSE ...;` | With `ELSE IF` chains and `DO; ... END;` blocks of the statements listed here |
| Subsetting `IF condition;` and `DELETE` | |
| `LENGTH name $ n;` | Before a variable's first use; numbers only with length 8 |
| Conditions | Comparisons (`=`, `^=`, `<`, `<=`, `>`, `>=` and `EQ`, `NE`, `LT`, `LE`, `GT`, `GE`); `AND`, `OR`, `NOT (...)`; `IN`, `NOT IN`; `BETWEEN ... AND`; `IS [NOT] MISSING` and `IS [NOT] NULL`; a number on its own. A comparison or condition used as a value is 1 or 0 |
| Comments | `/* ... */` anywhere, and `* ... ;` statements |

Everything else is rejected with a list of every unsupported construct, for
example `RETAIN`, `BY` with `FIRST.`/`LAST.`, `OUTPUT`, `MERGE`, arrays, `DO`
loops, functions, `**`, `||`, date constants, variable lists such as `x1-x5`,
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

### Statements run in order

A DATA step runs its statements in order for each row, so an assignment sees
the ones before it. SparkShift writes each assignment as a `.withColumn(...)`,
in the same order: an existing column keeps its place and a new one is added at
the end, as SAS adds new variables after the input's.

A subsetting `IF` and `DELETE` act where they appear, after the assignments
before them; `WHERE` filters the input, before any statement.

### Arithmetic and division by zero

*"SAS Operators in Expressions":* "If a missing value is an operand for an
arithmetic operator, the result is a missing value", as in Spark.

*SAS Users blog, "Dividing by zero with SAS – myths and realities":* SAS "sets
the result of division by zero to a missing value" and writes a note to the
log. Spark would raise an error, so `/` becomes `F.try_divide(...)`, which
returns NULL.

### Conditions that are numbers

*"SAS Operators in Expressions", Boolean Numeric Expressions:* "any numeric
value other than 0 or missing is true, and a value of 0 or missing is false."
So `if net;` becomes `F.col("net").isNotNull() & (F.col("net") != F.lit(0))`.
A comparison used as a value, as in `big = (amount > 100);`, is 1 or 0.

### IF without ELSE

*Language Reference: Concepts, "Combining SAS Data Sets: Methods":* "Variables
that are created by the DATA step are set to missing at the beginning of each
iteration of the DATA step. Variables that are read from a data set are not."

So `if amount > 100 then big = 1;` leaves `big` missing on other rows if the
step creates it, but leaves an input variable's value unchanged. SparkShift
cannot see the input's columns, so it uses what the program shows:

- a variable read before it is assigned (`if amount < 0 then amount = 0;`), or
  listed by `KEEP=` on the `SET` data set, is an input variable;
- with `KEEP=` on the `SET` data set, a variable it does not list is new.

Otherwise the assignment is rejected; adding an `ELSE` makes the meaning
explicit.

An `IF ... ELSE IF ... ELSE` chain that assigns one variable becomes a single
`F.when(...).when(...).otherwise(...)`. Other blocks assign each variable under
its condition. When a block changes a variable its condition reads, the
condition is first saved in a helper column (`_if_1`), dropped afterwards, so
it keeps its value from before the block.

### Text length

*SAS 9.4 Programmer's Guide: Essentials, "Examples: Create and Modify SAS
Variables":* the length of a new text variable "is set to the length of the
character value" first assigned to it; "you must use the longest possible value
in the first statement that uses the variable", because "you cannot change the
length with a subsequent LENGTH statement". When SAS assigns a value, "it pads
the value with blanks or truncates the value on the right side, if necessary".

So after `if amount > 100 then size = 'large'; else size = 'medium';`, SAS
stores `'mediu'`. SparkShift does the same and warns. A `LENGTH` statement
before the first assignment sets the length instead. When SparkShift cannot
tell whether a variable is new (and so whether its first value sets its
length), and a later value could be truncated, it rejects the assignment and
suggests a `LENGTH` statement.

## Limitations

- **Column order of KEEP:** SAS keeps variables in the data set's order;
  SparkShift cannot see that order and keeps them in the listed order. It warns
  whenever a `KEEP` names several variables.
- **Variable types:** SparkShift cannot see whether an input variable is a
  number or text, which SAS compares differently. A comparison needs a constant,
  arithmetic, or a variable the step assigns on one side to tell; comparing two
  input variables is rejected.
- **Input text lengths:** SparkShift cannot see an input text variable's length,
  so it never truncates values assigned to one.
- **Number types:** SAS stores every number as a double; the generated code
  keeps Spark's types (an integer stays an integer), with the same values.
- **Text order:** ordering comparisons on text (`<`, `>`) ignore trailing
  blanks. They differ from SAS only for text that ends in characters that sort
  below a blank, such as tabs.
- **Values pass through unchanged:** a NULL text value stays NULL in the
  result, where SAS would show a blank.
- **NOT needs parentheses:** in SAS, `NOT` binds more tightly than a comparison
  (`not x = 1` means `(not x) = 1`). SparkShift requires `NOT (...)`, so the
  meaning is never in doubt.
