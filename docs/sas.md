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

All rules come from SAS's public documentation at documentation.sas.com:
*SAS 9.4 Language Reference: Concepts*, *SAS 9.4 Functions and CALL Routines:
Reference* for functions, *Base SAS 9.4 Procedures Guide* for procedures, the
FREQ procedure chapter of the *SAS/STAT User's Guide*, and the *SAS SQL
Procedure User's Guide*.

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
| Functions | `SUM`, `MEAN`, `MIN`, `MAX`, `N`, `NMISS`, `COALESCE`; `ROUND`, `INT`, `CEIL`, `FLOOR`, `ABS`, `MOD`; `UPCASE`, `LOWCASE`, `STRIP`, `TRIM`, `SUBSTR`, `LENGTH`; `CATS`, `CATX`, `COALESCEC`; `MISSING`. See [Functions](#functions) |
| `PROC SORT` | `DATA=`, `OUT=` (with data set options), `NODUPKEY`, `BY` with `DESCENDING`, and `WHERE`. Options that do not change the result (`EQUALS`, `FORCE`, `TAGSORT`, `SORTSIZE=`, ...) are accepted. See [PROC SORT](#proc-sort) |
| `PROC SQL` | `CREATE TABLE name AS SELECT` with `DISTINCT`, columns and `AS` names, `FROM` a data set (with data set options and an alias), `INNER JOIN` and `LEFT JOIN ... ON`, `WHERE`, `GROUP BY` columns, `HAVING`, `ORDER BY` names or positions with `DESC`, and the summary functions `COUNT(*)`, `COUNT`, `SUM`, `AVG`/`MEAN`, `MIN`, `MAX`, `NMISS`, `RANGE`, `STD`, `VAR` (`COUNT` and `SUM` also with `DISTINCT`). See [PROC SQL](#proc-sql) |
| `PROC FREQ` | `DATA=`, `BY`, `WHERE`, and `TABLES a` or `TABLES a*b*...` with `/ OUT=`, `MISSING`, and `OUTPCT` (two-way tables). Options that only change printed output are accepted. See [PROC FREQ](#proc-freq) |
| `PROC MEANS`, `PROC SUMMARY` | `DATA=`, `NWAY`, `MISSING`; `CLASS` (with `/ MISSING`), `VAR`, `BY`, `WHERE`; `OUTPUT OUT=` with `N`, `NMISS`, `SUM`, `MEAN`, `MIN`, `MAX`, `RANGE`, `STD`, and `VAR`, in the forms `mean=`, `mean=names`, and `mean(variables)=names`. Options that only change printed output are accepted. See [PROC MEANS and SUMMARY](#proc-means-and-summary) |
| Comments | `/* ... */` anywhere, and `* ... ;` statements |

Everything else is rejected with a list of every unsupported construct, for
example `RETAIN`, `BY` with `FIRST.`/`LAST.`, `OUTPUT`, `MERGE`, arrays, `DO`
loops, other functions (date functions among them), `**`, `||`, date
constants, variable lists such as `x1-x5` and `OF x1-x5`, macros (`%let`,
`&name`), global statements such as `LIBNAME` and `OPTIONS`, `DATA _NULL_`,
writing to a permanent library, PROC SORT's `DUPOUT=`, `NOUNIQUEKEY`, and
collating sequences, PROC MEANS without `OUTPUT` (a printed report), quantiles
such as `MEDIAN`, `AUTONAME`, `TYPES`, `WAYS`, `ID`, `FREQ`, and `WEIGHT`, PROC
FREQ without `OUT=` (printed tables), its `OUTPUT` statement, `SPARSE`,
`OUTCUM`, and grouped requests such as `(a b)*c`, PROC SQL queries that are not
in `CREATE TABLE ... AS` (they print a report), subqueries, `CALCULATED`,
`UNION` and the other set operators, `RIGHT`, `FULL`, `CROSS`, and comma joins,
`INSERT`, `UPDATE`, and the other PROC SQL statements, and the other PROC
steps.

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

### Functions

*SAS 9.4 Functions and CALL Routines: Reference*, one page per function. A
function's arguments must have the type it takes: a number where it takes
numbers and text where it takes text (a variable of unseen type is taken to
have the right one). SAS would convert the other type and write a note;
SparkShift rejects it. Function results have a known type, so they can be
compared with each other: `upcase(a) = upcase(b)` is supported.

**Statistics skip missing values.** `SUM`, `MEAN`, `MIN`, and `MAX` use the
arguments that are present, and are missing only when all of them are
("returns the sum of the nonmissing arguments"; MIN "returns a missing value
(.) only if all arguments are missing"). `N` and `NMISS` count the arguments
that are present and missing; `COALESCE` returns the first one present. Note
that `MIN(x, 1)` is 1 for a missing `x`, while `x < 1` is true: the functions
skip missing values, the operators treat them as the smallest.

| SAS | PySpark |
|---|---|
| `sum(a, b)` | `F.when(F.coalesce(a, b).isNotNull(), F.coalesce(a, F.lit(0)) + F.coalesce(b, F.lit(0)))` |
| `sum(a, 1)` | `F.coalesce(a, F.lit(0)) + F.lit(1)`: never missing |
| `mean(a, b)` | `F.try_divide(<the sum>, a.isNotNull().cast("int") + b.isNotNull().cast("int"))` |
| `min(a, b)`, `max(a, b)` | `F.least(a, b)`, `F.greatest(a, b)`, which skip NULLs |
| `n(a, b)`, `nmiss(a, b)` | `a.isNotNull().cast("int") + ...`, `a.isNull().cast("int") + ...` |
| `coalesce(a, b)` | `F.coalesce(a, b)` |

**Rounding.** *ROUND:* "The ROUND function returns the value that is based on
decimal arithmetic": `ROUND(x, 0.01)` of the double nearest 2.675, which is
slightly below it, is 2.68. When the value is halfway between two multiples,
"the ROUND function rounds up the absolute value and restores the original
sign": `ROUND(-2.5)` is -3. Spark's `F.round` does the same on doubles (it
rounds the value's shortest decimal form, half away from zero), so
`round(x, 0.01)` becomes `F.round(x, 2)` and `round(x, 100)` becomes
`F.round(x, -2)`. SparkShift supports rounding units that are powers of 10,
down to 1E-15; SAS handles other units, such as 0.25, with extra steps that
have no Spark equivalent, so they are rejected.

*INT, CEIL, FLOOR:* "If the argument is within 1E-12 of an integer, the
function returns that integer." Spark has no such fuzzing: `CEIL` of
`0.1 * 3 * 10`, stored as 3.0000000000000004, is 4 in Spark and 3 in SAS. So
`ceil(x)` becomes `F.ceil(x - F.lit(1e-12))` and `floor(x)` becomes
`F.floor(x + F.lit(1e-12))`. `INT` truncates toward zero: "If the value of
argument is positive, the INT function has the same result as the FLOOR
function. If the value of argument is negative, the INT function has the same
result as the CEIL function."

*MOD:* "When the result is nonzero, the result has the same sign as the first
argument. The sign of the second argument is ignored", as with Spark's `%`.
The divisor "cannot be 0"; SparkShift needs it to be a constant. For
arguments that are not whole numbers, SAS makes a remainder that is very
close to 0 or to the divisor exactly 0 (`MOD(0.3, 0.1)` is 0), without saying
how close. SparkShift warns when the divisor is not a whole number.

**Text.** `UPCASE`, `LOWCASE`, `STRIP` (leading and trailing blanks), and
`TRIM` (trailing blanks) become `F.upper`, `F.lower`, `F.trim`, and
`F.rtrim`. *LENGTH:* "returns an integer that represents the position of the
rightmost non-blank character", and "if the value of string is blank, LENGTH
returns a value of 1", so it becomes
`F.greatest(F.length(F.rtrim(x)), F.lit(1))`; missing text is blank, so it is
1 too. *SUBSTR:* "If you omit length, SAS extracts the remainder of the
expression"; SparkShift supports constant positions and lengths.

*CATS, CATX:* CATS removes leading and trailing blanks from each item and
joins them. CATX also puts the delimiter between items, but "if item-i
contains at least one non-blank character": "Blank items do not produce
delimiters at the beginning or end of the result, nor do blank items produce
multiple consecutive delimiters." So `catx(', ', a, b)` becomes
`F.concat_ws(", ", F.nullif(F.trim(a), F.lit("")), ...)`: `concat_ws` skips
NULLs. The delimiter must be a text constant, used as written. Items must be
text: SAS writes numbers with the BESTw. format first, which SparkShift does
not reproduce.

*COALESCEC:* "A character value is considered missing if it has a length of
zero or if all the characters are blank. If all the values of all the
arguments are missing, the COALESCEC function returns a string with a length
of zero."

*MISSING:* returns 1 for a missing number, and for text "that contains all
blanks or has a length of zero"; 0 otherwise. In a condition it becomes the
same test as `IS MISSING`.

**Lengths of function results.** A new text variable gets its length from the
first value assigned to it (see [Text length](#text-length)). For a function,
that is the length SAS gives its result:

- `UPCASE`, `LOWCASE`, `STRIP`, `TRIM`, and `SUBSTR`: "that variable is given
  the length of the argument" (the first argument for `SUBSTR`). That length
  is known when the argument is a constant or a variable the step assigns.
- `CATS` and `CATX`: "that variable is given a length of 200 bytes". When the
  result is longer than the variable, SAS "changes the result to a blank value
  in the DATA step" rather than truncating it, so after
  `length label $ 6;`, `label = catx(' ', name, country);` is
  `F.when(F.length(<the CATX>) > F.lit(6), F.lit("")).otherwise(<the CATX>)`.
- `COALESCEC`: SparkShift treats the length as unknown.

### PROC SORT

*Base SAS 9.4 Procedures Guide*, "PROC SORT Statement", "BY Statement", and
"Concepts: SORT Procedure".

**Order.** "PROC SORT first arranges the data set by the values in ascending
order, by default, of the first BY variable", then the observations with the
same value by the next BY variable, and so on. `DESCENDING` "reverses the sort
order for the variable that immediately follows". Numbers sort in this order:
"SAS missing values", "negative numeric values", "zero", "positive numeric
values". So missing values come first, as Spark's `.asc()` puts NULLs, and
last with `DESCENDING`, as `.desc()` does:

```sas
proc sort data=orders out=sorted;
    by customer_id descending amount;
run;
```

becomes `.orderBy(F.col("customer_id").asc(), F.col("amount").desc())`.
Text sorts in the ASCII order on ASCII systems: "digits are sorted before
uppercase letters, and uppercase letters are sorted before lowercase letters.
The blank is the smallest character that you can display." Spark compares
text by its UTF-8 bytes, which is the same order for ASCII text.

**Data sets.** `OUT=` names the sorted data set; "without the OUT= option,
PROC SORT replaces the original data set with the sorted observations", so
the DataFrame variable is assigned again. Without `DATA=`, PROC SORT sorts the
data set created last (`_LAST_`). The `WHERE` statement filters the input;
`DATA=` and `OUT=` options work as in DATA steps, the `OUT=` options after the
sort.

**NODUPKEY** writes "only the first observation of each BY group to the output
data set", and becomes `.dropDuplicates([...])` before the sort. SAS's first
observation is the first in the input's order, but a Spark DataFrame has no
such order, so `dropDuplicates` keeps any row of each group. SparkShift warns,
unless `KEEP=` on the `DATA=` data set shows that every variable is a BY
variable; then all rows of a group are the same.

### PROC MEANS and SUMMARY

*Base SAS 9.4 Procedures Guide*, "MEANS Procedure": the PROC MEANS, CLASS,
VAR, and OUTPUT statements, "Results: PROC MEANS", and "Keywords and
Formulas". PROC SUMMARY works the same way, but prints nothing.

**What is computed.** Each `OUTPUT OUT=` statement creates a data set with
"the variables specified in the BY statement", "the variables specified in
the CLASS statement", `_TYPE_`, `_FREQ_` ("the number of observations that a
given output level represents"), and the statistics, in that order:

```sas
proc means data=orders noprint nway;
    class status;
    var amount;
    output out=summary sum=revenue mean=average;
run;
```

becomes `.groupBy(F.col("status")).agg(F.lit(1).alias("_TYPE_"),
F.count(F.lit(1)).alias("_FREQ_"), F.sum(...).alias("revenue"), ...)`. The
statistics follow the keyword definitions: `N` is "the number of x values
that are not missing" (`F.count`), `NMISS` the number that are missing
(`F.count_if`), `RANGE` "the difference between maximum value and minimum
value", and `VAR` and `STD` divide by n − 1, the default `VARDEF=DF`
(`F.var_samp`, `F.stddev_samp`). "PROC MEANS excludes missing values for the
analysis variables before calculating statistics", as Spark's aggregates do.

**Names.** In `statistic(variables)=names`, "the first name contains the
statistic for the first analysis variable", and so on. Without variables, the
statistic is for the `VAR` variables; without names, the output variables take
the analysis variables' names. SparkShift needs one name for each variable, or
none, and different names for all output variables.

**Missing CLASS values.** "If a class variable has a missing value for an
observation, then PROC MEANS excludes that observation from the analysis
unless you use the MISSING option", so SparkShift filters those rows first. "If
a BY or ID variable value is missing, then PROC MEANS treats it like any other
BY or ID variable value."

**_TYPE_ and NWAY.** "By default, PROC MEANS generates all possible types":
every combination of the CLASS variables, which is Spark's `.cube(...)`.
`_TYPE_` "indicates which combination of the class variables PROC MEANS uses":
in binary, "each value of one indicates an active class variable in the type",
the first CLASS variable the leftmost digit. Spark's `F.grouping_id()` has the
same bits with ones for the variables that are not active, so `_TYPE_` is
`2^k − 1 − grouping_id()` for k CLASS variables. `NWAY` "limits the output
statistics to the observations with the highest _TYPE_ value", which is a
`.groupBy(...)` with `_TYPE_` set to `2^k − 1`.

**Order.** The output is sorted by the BY variables, then by `_TYPE_` (the
`DESCENDTYPES` option "orders observations in the output data set by
descending _TYPE_ value"), then by the CLASS variables' values (the default,
`ORDER=UNFORMATTED`, "orders values by their unformatted values"). BY groups
come in the BY variables' order, as the input must be sorted by them.

### PROC FREQ

*SAS/STAT User's Guide*, "The FREQ Procedure": the PROC FREQ and TABLES
statements, "Missing Values", and "Output Data Sets".

**What is computed.** "The OUT= option in the TABLES statement creates an
output data set that contains one observation for each combination of
variable values (or table cell) in the last table request", with the BY
variables, the table's variables, `COUNT` ("the table cell frequency"), and
`PERCENT` ("the table cell percentage"). With `OUTPCT`, a two-way table also
gets `PCT_ROW` and `PCT_COL`, the percentages of the row and column
frequencies. Percentages are within each BY group: a window sum over the BY
variables (`F.sum(...).over(Window.partitionBy(...))`).

**Missing values.** "If an observation has a missing value for a variable in a
TABLES request, by default PROC FREQ does not include that observation in the
frequency or crosstabulation table. Also by default, PROC FREQ does not include
observations with missing values in the computation of percentages", but "the
TABLES statement OUT= data set includes an observation that contains the
missing value frequency". So a combination with a missing value keeps its
`COUNT`, with missing percentages, and the other percentages add up to 100.
SparkShift computes this with a helper column, `_counted`, that holds the
counts that take part (dropped afterwards). `MISSING` "treats missing values
as a valid nonmissing level for all TABLES variables", included in the
percentages.

**Order.** By default, `ORDER=INTERNAL`: values in their unformatted order, and
"the ORDER= option does not apply to missing values, which are always ordered
first". The output is sorted by the BY variables, then by the table's
variables.

### PROC SQL

*SAS SQL Procedure User's Guide*: "Retrieving Rows That Satisfy a Condition",
"Sorting Data", "Summarizing Data", and "PROC SQL and the ANSI Standard". Why
PROC SQL does not go through SparkShift's SQL conversion:
[ADR 0006](adr/0006-proc-sql-follows-sas-rules.md).

**Missing values follow SAS, not SQL.** "PROC SQL follows the SAS convention
for handling missing values", and "PROC SQL treats missing values as smaller
than nonmissing values": the guide's own example of `where Depth lt 500`
returns features with a missing depth. So conditions in `WHERE`, `ON`, and
`HAVING` follow the same rules as a DATA step's `WHERE` (see
[Missing numbers](#missing-numbers) and [Text](#text)), not SQL's three-valued
logic: in PROC SQL, `score < 3` keeps missing scores, which SparkShift's SQL
conversion would drop.

**Joins.** A comparison of two values is true when both are missing, so a join
condition `a.id = b.id` matches missing values with each other, which
`eqNullSafe` does. SparkShift cannot see whether the columns are numbers or
text; for text, SAS would also ignore trailing blanks and treat empty text as
missing. So an equality of two columns is translated with a warning; other
comparisons of two columns are rejected, as in a DATA step.

**Summary functions.** In a query's `SELECT` and `HAVING`, `COUNT(*)` counts
rows, and a summary function with one argument, such as `SUM(amount)`,
summarizes the rows of each group, skipping missing values. With two or more
arguments, `SUM`, `MIN`, `MAX`, `MEAN`, `N`, and `NMISS` are the DATA step
functions, computed for each row: `sum(amount, discount)` adds the two values
of the row. Every selected column must be grouped or summarized; SAS would
otherwise "remerge" the summary with each row, which SparkShift rejects. A
`HAVING` condition becomes a helper column, `_having`, computed with the
summaries and dropped after filtering. `GROUP BY` keeps missing values as a
group, unlike PROC MEANS's `CLASS`.

**Names and order.** A selected column keeps its name; a computed one needs
`AS` (SAS would name it `_TEMA001`). "PROC SQL sorts nulls, or missing values,
before character or numeric data": missing values come first in `ORDER BY`,
and last with `DESC`, as Spark's `.asc()` and `.desc()` place NULLs.

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
- **Characters, not bytes:** SAS's `LENGTH` function and variable lengths count
  bytes; Spark counts characters. They differ for text that is not ASCII, such
  as `'Café'` (4 characters, 5 bytes in UTF-8).
- **Upper and lower case:** `F.upper` and `F.lower` change the case of every
  Unicode letter. SAS's result for letters outside the English alphabet
  depends on its encoding and locale settings.
- **Long CATS and CATX results:** a result longer than 200 characters, stored
  in a new variable, is blank in SAS; SparkShift keeps it.
- **CATS and CATX of numbers:** a variable of unseen type is taken to be text.
  If it is a number, SAS writes it with the BESTw. format, which can differ from
  Spark's text form (for example for 0.1 + 0.2).
- **Rounding near a half:** SAS also rounds away from zero values that are
  only approximately halfway, within about 1E-12 of the value, such as
  0.4999999999999; Spark rounds them down.
- **MOD of fractions:** see [Functions](#functions); SparkShift warns.
- **SUBSTR past the end:** SAS documents a length that runs past the end (it
  takes the rest), but not a position past the end of the variable. Spark
  returns an empty string, which compares as blank.
- **Ties in a sort:** SAS's sort keeps the input's order for observations with
  the same BY values (the default `EQUALS` "maintains the relative order of
  the observations within the input data set"). Spark's sort does not, so
  such rows can come in any order.
- **Blank text in a sort:** for SAS, missing text, empty text, and text that
  differs only in trailing blanks are equal. Spark sorts NULL before an empty
  string, and `'a'` before `'a '`; with `NODUPKEY`, they are different BY
  values.
- **Order in later steps:** a SAS data set keeps its order. Spark does not
  promise that later operations keep a sorted DataFrame's order; sort it again
  just before writing it out if the order matters.
- **Formatted CLASS values:** SAS groups CLASS variables by their formatted
  values ("a single level consists of all input observations whose formatted
  class values match"), BEST12. for a number without a format. Spark groups by
  the values themselves, so numbers that differ only beyond 12 significant
  digits are one SAS level but separate Spark groups. The same applies to
  text that differs only in trailing blanks, and to missing versus empty text
  with `MISSING`.
- **BY with CLASS:** without `NWAY`, SAS computes every combination of the
  CLASS variables within each BY group; SparkShift requires `NWAY` then.
- **Missing combinations in FREQ tables:** for a table of several variables,
  SAS's documentation describes "an observation that contains the missing value
  frequency" without saying how combinations that are partly missing are
  grouped. SparkShift writes one row for each combination of values that has a
  missing value, as for a one-way table, each with missing percentages.
- **Counting text in PROC SQL:** `COUNT(x)` and `NMISS(x)` count NULLs as
  missing; for SAS, empty text is missing too.
- **VAR is needed:** without a `VAR` statement, SAS analyzes every numeric
  variable, which SparkShift cannot see; name the variables in `VAR` or in
  `statistic(variables)=`.
