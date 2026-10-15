# Supported SQL

What SparkShift converts from generic SQL and its six dialects (T-SQL,
PostgreSQL, MySQL, Snowflake, BigQuery, and Oracle), how it handles their
differences from Spark, and the known limitations. SAS is covered in
[sas.md](sas.md). Anything not listed as supported is rejected with an error
that names the construct, rather than converted into code that might be wrong.

## Constructs

| Construct | Status |
|---|---|
| `SELECT ... FROM table` (including `schema.table`, quoted names, and table aliases) | Supported |
| Columns, qualified columns (`t.column`), `*`, `t.*`, and `AS` aliases | Supported |
| Wildcard modifiers: `* EXCEPT`/`EXCLUDE`, `REPLACE`, `RENAME`, and `ILIKE`, including modified `COUNT(*)` | Unsupported — rejected with rewrite guidance; never treated as an unchanged wildcard |
| Joins: `[INNER] JOIN`, `LEFT`/`RIGHT`/`FULL [OUTER] JOIN`, `CROSS JOIN`, comma joins | Supported, with `ON` or `USING` |
| Literals: integers, decimals, doubles (`1.5e0`), strings, `TRUE`/`FALSE`, `NULL` | Supported |
| Arithmetic: `+ - * / %` and unary `-` | Supported, with dialect exceptions below |
| Comparisons: `= <> != < <= > >=` | Supported |
| Logic: `AND OR NOT` (SQL three-valued logic with NULL) | Supported |
| `WHERE` | Supported |
| `DISTINCT` | Supported (`DISTINCT ON` is not) |
| Row limits: `LIMIT n`, T-SQL `TOP n`, `FETCH FIRST n ROWS ONLY` | Supported, for a constant `n` |
| `ORDER BY` columns, aliases, positions (`ORDER BY 2`), expressions, and aggregates, with `ASC`/`DESC` and `NULLS FIRST`/`NULLS LAST` | Supported, with each dialect's NULL placement |
| Aggregates: `COUNT(*)`, `COUNT`, `COUNT(DISTINCT ...)`, `SUM`, `SUM(DISTINCT ...)`, `AVG`, `MIN`, `MAX` | Supported |
| `GROUP BY` columns, positions (`GROUP BY 1`), and `HAVING` | Supported, with dialect exceptions below |
| `IN (...)`, `BETWEEN`, `LIKE`, `ILIKE`, `IS [NOT] NULL`, `IS [NOT] DISTINCT FROM`, `<=>` | Supported (constant `LIKE` patterns) |
| `CASE` (searched and simple), `IF`, `IIF`, `COALESCE`, `IFNULL`, `NVL`, `NULLIF` | Supported |
| `CAST`, `TRY_CAST`, `::` to integer types, `DECIMAL(p, s)`, `DOUBLE`, `VARCHAR`/`TEXT`/`STRING`, `DATE`, `BOOLEAN` | Supported |
| String functions: `UPPER`, `LOWER`, `LENGTH`/`LEN`/`CHAR_LENGTH`, `TRIM`/`LTRIM`/`RTRIM`, `SUBSTRING`/`SUBSTR`, `CONCAT`, `\|\|`, `REPLACE`, `LEFT`, `RIGHT` | Supported, with dialect rules below |
| Numeric functions: `ABS`, `ROUND`, `CEIL`/`CEILING`, `FLOOR`, `POWER`, `SQRT`, `SIGN`, `LN`, `LOG(base, x)`, `EXP`, `GREATEST`, `LEAST` | Supported, with dialect rules below |
| Date parts: `EXTRACT`/`DATEPART`/`YEAR`/`MONTH`/`DAY`/… for year, quarter, month, day, hour, minute | Supported |
| Date arithmetic in days, weeks, months, years: `+`/`-` `INTERVAL`, `DATEADD`, `DATE_ADD`, `DATE_SUB` | Supported; keeps the input type |
| Day differences: `DATEDIFF` and `DATE_DIFF` in days | Supported, with each dialect's argument order |
| `DATE_TRUNC` to year, quarter, month; `CURRENT_DATE`, `CURRENT_TIMESTAMP`; casts to timestamp types | Supported, with dialect rules below |
| Window functions: `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `NTILE`, `LAG`, `LEAD`, `FIRST_VALUE`, `LAST_VALUE`, and `COUNT`/`SUM`/`AVG`/`MIN`/`MAX` `OVER (PARTITION BY ... ORDER BY ...)` | Supported in the `SELECT` list of queries without `GROUP BY` |
| Window frames: `ROWS BETWEEN` with `UNBOUNDED`, `n PRECEDING`, `CURRENT ROW`, `n FOLLOWING`; `RANGE BETWEEN` with `UNBOUNDED` and `CURRENT ROW`; `IGNORE NULLS` for `FIRST_VALUE`/`LAST_VALUE` | Supported |
| `QUALIFY` on window functions, `SELECT` aliases, and columns | Supported in generic SQL, Snowflake, and BigQuery, in queries without `GROUP BY`; filters after the window functions and before `DISTINCT`, `ORDER BY`, and `LIMIT` |
| `NTH_VALUE`, `RANGE` frames with offsets, `GROUPS` frames, `EXCLUDE`, `IGNORE NULLS` for `LAG`/`LEAD`, named windows, and windows (including `QUALIFY`) in aggregate queries | Unsupported — rejected with an error |
| `WITH` (CTEs, including column lists) and subqueries in `FROM` and `JOIN` | Supported |
| `NATURAL`, semi, anti, and as-of joins; `LATERAL` and `APPLY` | Unsupported — rejected with an error |
| `UNION [ALL]`, `INTERSECT [ALL]`, `EXCEPT [ALL]` and `MINUS`, with `ORDER BY`/`LIMIT` on the result | Supported |
| Subqueries in expressions: `IN`/`NOT IN (subquery)` as `WHERE`/`HAVING` conditions; `EXISTS`, `NOT EXISTS`, and subqueries used as values in `WHERE`, `HAVING`, `ORDER BY`, and the `SELECT` list; correlated references to the enclosing query | Supported |
| `WITH RECURSIVE`, `UNION BY NAME`, `ORDER BY` expressions on a set operation, `ANY`/`ALL`/`SOME` comparisons, and row-value `IN (subquery)` | Unsupported — rejected with an error |
| `GROUP BY` expressions, `ROLLUP`, `CUBE`, `GROUPING SETS`, `AVG(DISTINCT ...)` | Unsupported — rejected with an error |
| `LIKE ... ESCAPE`, `IS TRUE`/`IS FALSE`, casts to `FLOAT`/`REAL`, `CHAR(n)`/`VARCHAR(n)`, unparameterized `DECIMAL`, and timestamps | Unsupported — rejected with an error |
| Everything else, including `OFFSET`, user-defined functions, date formatting and parsing, and other functions | Unsupported — rejected with an error |

Support grows feature by feature; see the [roadmap](../ROADMAP.md).

## Dialect emulations

Where a source dialect's function behaves differently from Spark's but an exact
PySpark equivalent exists, SparkShift generates code with the source dialect's
behavior:

| Construct | Dialect | Behavior | Generated as |
|---|---|---|---|
| `LEN(x)` | T-SQL | Ignores trailing spaces | `F.length(F.rtrim(x))` |
| `LENGTH(x)` | MySQL | Counts bytes, not characters | `F.octet_length(x)` |
| `CONCAT(...)` | PostgreSQL, T-SQL, Oracle | Skips NULL inputs | `F.concat_ws("", ...)` |
| `a \|\| b` | Oracle | Treats NULL as an empty string | `F.concat_ws("", a, b)` |
| `a \|\| b` | MySQL | Logical OR, not concatenation | `a \| b` |
| `DATEDIFF(day, start, end)` | T-SQL, Snowflake | Arguments in the opposite order to Spark | `F.datediff(end, start)` |
| `x + INTERVAL '3' DAY`, `DATEADD(day, 3, x)` | All except PostgreSQL | Keeps the input type (a timestamp keeps its time of day) | `x + F.make_interval(days=...)` |
| `CAST(x AS DATE) + INTERVAL '1 month'`, `CURRENT_DATE + INTERVAL ...` | PostgreSQL | A date plus an interval is a timestamp at midnight | `x.cast("timestamp_ntz") + F.make_interval(...)` |
| `DATE_TRUNC('month', x)` | PostgreSQL | Returns a timestamp | `F.date_trunc("month", x)` |
| `DATE_TRUNC(x, MONTH)` | BigQuery | Returns a date | `F.trunc(x, "month")` |
| `FIRST_VALUE`, `LAST_VALUE` without a frame | Snowflake | Cover the whole window | `.rowsBetween(Window.unboundedPreceding, Window.unboundedFollowing)` |
| `CAST(x AS INT)` and other integer types | PostgreSQL, MySQL, Snowflake, BigQuery | Round a fraction to the nearest integer (`2.7` → `3`); Spark's cast drops it | `F.round(x).cast("int")` |

## Timestamps and time zones

Spark has two timestamp types: `TIMESTAMP`, a point in time shown in the
session time zone, and `TIMESTAMP_NTZ`, a wall-clock time without a time zone.
Casts map each source type to the one with the same meaning:

| Source type | Meaning | Spark type |
|---|---|---|
| PostgreSQL, Snowflake, and Oracle `TIMESTAMP`; T-SQL `DATETIME2`; MySQL and BigQuery `DATETIME` | Wall-clock time | `timestamp_ntz` |
| `TIMESTAMPTZ`, `TIMESTAMP WITH TIME ZONE`, Snowflake `TIMESTAMP_LTZ`, MySQL and BigQuery `TIMESTAMP`, T-SQL `DATETIMEOFFSET` | Point in time | `timestamp` |
| Generic SQL `TIMESTAMP` | Spark's own meaning | `timestamp` |

Snowflake's `TIMESTAMP` follows its default `TIMESTAMP_TYPE_MAPPING` of
`TIMESTAMP_NTZ`. Values are interpreted in the Spark session time zone, which
should match the time zone the source data was produced in.

## Sorting and NULLs

Databases disagree on where NULLs go when sorting. SparkShift reads the
placement from the source dialect and spells it out in the generated code
when it differs from Spark's default, for example `.asc_nulls_last()`:

| Dialects | `ORDER BY x` | `ORDER BY x DESC` |
|---|---|---|
| Spark, generic SQL, T-SQL, MySQL, BigQuery | NULLs first | NULLs last |
| PostgreSQL, Oracle, Snowflake | NULLs last | NULLs first |

The same applies to `ORDER BY` inside a window. An explicit `NULLS FIRST`
or `NULLS LAST` always wins. A plain `ORDER BY` name means a `SELECT` alias
before a same-named table column, as standard SQL specifies.

## Subqueries and NULLs

`x NOT IN (SELECT ...)` is never true when the subquery returns a NULL, so
it returns no rows; and a row whose `x` is NULL never qualifies. SparkShift
translates it with Spark's own subquery support (`~F.col("x").isin(subquery)`),
which follows these rules, rather than a `left_anti` join, which does not.

Used as a value rather than as a `WHERE` or `HAVING` condition (in the
`SELECT` list, or inside `OR`), `IN (subquery)` is rejected: where SQL
returns NULL, Spark returns false. `EXISTS` is never NULL, so it can be used
anywhere. A correlated subquery must qualify the enclosing query's columns
with its table name or alias, as in `o.customer_id = c.customer_id`, and can
refer only to the query directly around it.

## Window frames

Without an explicit frame, a window function with `ORDER BY` covers the rows
up to the current row and every row tied with it, as standard SQL and Spark
specify. So `LAST_VALUE(x) OVER (ORDER BY d)` is usually the current row's
own value; add `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING` for
the window's last value. Snowflake documents the whole window as the default
for `FIRST_VALUE` and `LAST_VALUE`, and SparkShift writes that frame out.
BigQuery does not document this default, so these functions need an explicit
frame there.

## Dialect differences

SparkShift generates code with Spark semantics. Where a construct in the
source dialect means something different in Spark, has no Spark equivalent, or
could mean two things that only the table schema would distinguish, SparkShift
rejects it rather than guess:

| Construct | Dialects | Why it is rejected |
|---|---|---|
| `/` | T-SQL, PostgreSQL | Dividing two integers discards the remainder; Spark returns a fraction. |
| `/` | MySQL | Division by zero returns NULL; Spark raises an error. |
| `+` | T-SQL | `+` also concatenates strings. |
| `WHERE` referring to a `SELECT` alias | Snowflake | Spark does not allow it, and the name could also be a real column. |
| `ROWNUM`, `ROWID` | Oracle | Pseudo-columns with no Spark equivalent; use `FETCH FIRST n ROWS ONLY`. |
| `(+)` outer-join marker | Oracle | Ignoring it would turn an outer join into an inner join; use `LEFT`/`RIGHT JOIN`. |
| `TOP n PERCENT`, `WITH TIES`, `OFFSET` | T-SQL, Oracle, others | Not equivalent to a plain row limit. |
| `INTERSECT` mixed with `UNION` or `EXCEPT` without parentheses | All | Standard SQL and Spark run `INTERSECT` first; Oracle runs set operators left to right. Add parentheses. |
| `GROUP BY 1` | Oracle, T-SQL | Oracle groups by the constant 1; T-SQL does not allow positions. |
| `GROUP BY` or `HAVING` naming a `SELECT` alias | All | Databases differ on whether a same-named column wins, and only the schema would tell. |
| A `SELECT` alias inside an `ORDER BY` expression (`ORDER BY total * 2`) | All | Some databases, such as PostgreSQL, read the name as a table column instead. |
| Selecting a column that is neither grouped nor aggregated | MySQL (relaxed mode) | MySQL returns an arbitrary value; Spark raises an error. |
| `ISNULL(a, b)` | T-SQL | Returns the first argument's type (`ISNULL(int_col, 1.5)` is 1); `COALESCE` returns 1.5. |
| `AVG(x)` unless `x` is visibly a decimal | T-SQL | The average of integers is an integer (`AVG` of 1 and 2 is 1); Spark returns 1.5. Write `AVG(CAST(x AS DECIMAL(p, s)))`. |
| `CAST(x AS VARCHAR)` without a length | T-SQL | Means `VARCHAR(30)` and truncates longer values. |
| `CAST(x AS FLOAT)` / `REAL` | All except PostgreSQL `FLOAT` | Sizes differ: T-SQL `FLOAT` is 8 bytes, Spark `FLOAT` is 4. (PostgreSQL's `FLOAT` means `DOUBLE PRECISION` and is converted to `double`.) |
| `LIKE '[a-c]%'` | T-SQL | `[ ]` is a character class in T-SQL; Spark matches it literally. |
| `LIKE` patterns containing `\` | All | Whether backslash escapes wildcards differs between databases and Spark. |
| `ROUND(x)` | PostgreSQL, MySQL, Oracle | Floating-point halves round to even (`2.5` → `2`) but exact numbers away from zero; Spark always rounds away, and only the column type would tell. |
| `ROUND(x, n, 1)` | T-SQL | The third argument truncates instead of rounding. |
| `GREATEST`, `LEAST` | MySQL, Oracle, Snowflake, BigQuery | Return NULL when any argument is NULL; Spark ignores NULL arguments. |
| `LOG(x)` | PostgreSQL, Snowflake, Oracle, generic | Base 10 in some databases, natural logarithm in others; write `LN(x)` or `LOG(base, x)`. |
| `TRIM` | BigQuery | Removes all Unicode whitespace, including tabs; Spark removes only spaces. |
| `SUBSTRING`, `LEFT`, `RIGHT` with negative or computed positions | All | Negative positions behave differently across databases. |
| `EXTRACT(SECOND ...)`, weekdays, week numbers | All | Fractions of a second, the first day of the week, and week numbering differ. |
| `DATEDIFF` in months, years, or other units | All | Databases count month and year boundaries differently. |
| `DATE_TRUNC` | Snowflake, T-SQL, Oracle, MySQL | Returns the input's type (or does not exist); only the schema would tell. |
| `ADD_MONTHS` | Oracle, Snowflake | Keeps the last day of the month (Feb 29 + 1 month = Mar 31); Spark does not. |
| `CAST(x AS DATETIME)`, `SMALLDATETIME` | T-SQL | Round to 1/300 of a second or to the minute. |
| `CAST(x AS TIMESTAMP)` | T-SQL | Means `ROWVERSION`, a binary row version. |
| `SYSDATE` | Oracle | No fractional seconds; uses the server's time zone. |
| `FIRST_VALUE`, `LAST_VALUE` with `ORDER BY` but no frame | BigQuery | The default frame is not documented. |
| `RANGE BETWEEN 5 PRECEDING AND ...` | All | The offset is measured in the `ORDER BY` column's type (a number, or an interval for dates), which only the schema would tell. |
| `ROW_NUMBER`, `RANK`, `DENSE_RANK`, `NTILE`, `LAG`, `LEAD` without `ORDER BY` | PostgreSQL, MySQL, others | The result depends on an arbitrary row order, and Spark requires an `ORDER BY`. |
| `QUALIFY` | T-SQL, PostgreSQL, MySQL, Oracle | These databases have no `QUALIFY` clause. |
| `column + INTERVAL ...` | PostgreSQL | The result is a timestamp if the column is a date, and keeps the column's type otherwise; only the schema would tell. Write `order_date + 3` to keep a date, or `CAST(order_date AS TIMESTAMP) + INTERVAL '3 days'`. |
| `QUALIFY` referring to a `SELECT` alias (`QUALIFY rn = 1`) | Snowflake | Snowflake reads the name as a table column when the table has one, which only the schema would tell; repeat the window function instead. Spark reports a name that could be both as ambiguous, and BigQuery's documentation filters on aliases, so there the name means the alias. |
| A `SELECT` alias inside a window function in `QUALIFY` | All | The window is computed before the `SELECT` list's aliases exist. |

## Known limitations

- Database-specific string collation is not emulated. For example, MySQL and
  SQL Server often compare and sort strings, including in `LIKE`, `REPLACE`,
  and `ORDER BY`, case-insensitively; Spark compares and sorts them
  case-sensitively, so `'Z'` sorts before `'a'`.
- SQL Server ignores trailing spaces when it compares strings with `=`, `<`,
  or `>` (ANSI padding; `LIKE` is the exception), so `'abc ' = 'abc'` is true
  there and false in Spark. Only the column types would tell where this
  applies; use `RTRIM` on both sides to compare the same way in both.
- MySQL's `AVG` of integers or `DECIMAL` values returns a `DECIMAL`, with a
  fixed number of decimal places; Spark returns a double. The averages differ
  only by MySQL's rounding of its last decimal place.
- Oracle treats an empty string as NULL; SparkShift does not emulate this.
- Casting an exact half of a floating-point value to an integer: PostgreSQL
  and MySQL round it to even (`2.5` → `2`, checked on both servers), while the
  generated `F.round(x)` rounds it away from zero (`3`), as both databases do
  for exact numbers such as `DECIMAL`. Only the column type would tell them
  apart. Snowflake does not document how it rounds halves.
- In PostgreSQL, subtracting two dates gives a number of days; in Spark it
  gives an interval. SparkShift cannot tell that two columns are dates.
- Date formatting and parsing with format strings (`TO_DATE(s, fmt)`,
  `FORMAT`, `TO_CHAR`) are not supported yet; each dialect uses its own codes.
- An unaliased `SELECT` item that calls a function, such as `SELECT UPPER(name)`,
  must be given an alias, because Spark names the column after the exact
  spelling used (`CEIL` or `CEILING`), which is lost in parsing.
- `CAST` failures follow Spark's ANSI behavior: an invalid value raises an
  error (use `TRY_CAST` for NULL instead), where some databases, such as MySQL,
  return a default value.
- An unaliased negation, `BETWEEN`, or `IF` in the `SELECT` list must be given
  an alias, because Spark SQL and PySpark name those output columns
  differently.
- A `JOIN` without `ON` or `USING` (accepted by MySQL) is translated as a cross
  join, because SQLGlot represents it exactly like the comma form `FROM a, b`.
- When an aggregate query's `SELECT` order differs from the `GROUP BY` order,
  or a key is not selected, unaliased aggregates must be given an alias.
- Window functions must be given an alias, because Spark names the column
  after the whole window definition.
- `ROW_NUMBER()` numbers rows that tie on the window's `ORDER BY` in an
  arbitrary order, in Spark as in every database. Add a unique column to the
  window's `ORDER BY` for repeatable results.
- In a query with window functions or `QUALIFY`, `ORDER BY` must use selected
  columns or aliases: computing a window regroups the rows, so sorting earlier
  would not last.
- `QUALIFY` is converted with helper columns for the window functions and
  unselected columns it uses, because Spark does not allow window functions
  in a filter. Helpers are dropped after filtering. With wildcard projections,
  the generated code inspects column names at execution to choose unused
  helper names; SAS IF blocks use the same protection unless `KEEP=` on `SET`
  lists the input's columns. Known outputs reserve selected aliases and
  grouping keys during conversion. See
  [ADR 0008](adr/0008-protect-generated-helper-column-names.md).
