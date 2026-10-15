# Python API

Everything below is importable from the `sparkshift` package. SparkShift has
one runtime dependency, SQLGlot, and never needs PySpark or a Spark session to
convert: it returns code as text.

## `convert(sql, dialect=None)`

Converts one SQL query, or a SAS program, into PySpark DataFrame code and
returns a `ConversionResult`.

- `sql`: the query or program text.
- `dialect`: `"tsql"`, `"postgres"`, `"mysql"`, `"snowflake"`, `"bigquery"`,
  `"oracle"`, or `"sas"`, case-insensitive; `None` (the default) for generic
  SQL. With `"sas"`, each data set the program creates becomes a DataFrame
  variable, and `result` is the last one.

```python
import sparkshift

result = sparkshift.convert("SELECT TOP 3 name FROM customers", dialect="tsql")
print(result.code)
for warning in result.warnings:
    print(warning)
```

The generated code imports what it uses, expects a SparkSession named `spark`
(as in a Databricks notebook), and assigns the final DataFrame to `result`.

## `ConversionResult`

The outcome of a successful conversion, an immutable dataclass:

| Field | Type | Meaning |
|---|---|---|
| `code` | `str` | The generated PySpark code |
| `warnings` | `tuple[Diagnostic, ...]` | Things to review before running the code, such as a SAS rule the code follows only partly; empty when there are none |

## `Diagnostic`

One finding about one construct, used for warnings and for the issues of
`UnsupportedSQLError`:

| Field | Type | Meaning |
|---|---|---|
| `message` | `str` | What the construct is, such as `function MY_UDF` |
| `sql` | `str` | The fragment of the input it is about |
| `hint` | `str \| None` | A suggested rewrite, when there is one |

`str(diagnostic)` gives `message: sql. Hint: hint`, with long fragments
shortened.

## `to_notebook(result, *, source=None, dialect=None, catalog="", schema="")`

Returns `result.code` as a Databricks notebook in source format (see
[databricks.md](databricks.md)). `source` names the file the code came from,
for the title; `dialect` is the language it was converted from, as given to
`convert`; `catalog` and `schema` are the initial values of the notebook's
`catalog` and `schema` widgets (empty keeps the current ones).

## Errors

Every error derives from `SparkShiftError`, so one `except` handles them all:

| Exception | Raised when | Attributes |
|---|---|---|
| `UnsupportedDialectError` | `dialect` is not supported | `dialect`, `supported` |
| `SQLParseError` | The input is empty or is not valid SQL | `message`, `line`, `column` (1-based; `None` when unknown) |
| `SASParseError` | The input is empty or is not a valid SAS program; a subclass of `SQLParseError` | as `SQLParseError` |
| `MultipleStatementsError` | The SQL contains more than one statement | `count` |
| `UnsupportedSQLError` | The input is valid but uses constructs SparkShift cannot translate safely | `issues`: every unsupported construct, as `Diagnostic`s |

```python
try:
    sparkshift.convert(sql, dialect="snowflake")
except sparkshift.UnsupportedSQLError as error:
    for issue in error.issues:
        print(issue.message, issue.hint)
```

## Version

`sparkshift.__version__` is the installed version.
