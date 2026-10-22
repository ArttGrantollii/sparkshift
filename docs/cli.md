# Command line

The `sparkshift` command has three subcommands: `convert` writes PySpark code
or notebooks, `report` assesses how much of a workload converts, and `upload`
imports notebooks into a Databricks workspace. `python -m sparkshift` works
the same way; in a development checkout, run it as `uv run sparkshift`.
`sparkshift COMMAND --help` lists every option.

## convert

```bash
sparkshift convert query.sql                    # print the PySpark code
sparkshift convert query.sql --dialect tsql     # SQL in a specific dialect
sparkshift convert query.sql -o query.py        # write the code to a file
cat query.sql | sparkshift convert -            # read standard input
sparkshift convert queries/ -o pyspark/         # every .sql and .sas file in a directory
sparkshift convert queries/ -o notebooks/ --notebook   # Databricks notebooks
sparkshift convert queries/ -o pipeline/ --pipeline    # Lakeflow pipeline source files
```

For example:

```bash
echo "SELECT TOP 3 name FROM customers ORDER BY name" | sparkshift convert - --dialect tsql
```

<!-- Exact SparkShift output, checked by tests/test_readme.py. Not reformatted. -->
<!-- fmt: off -->
```python
from pyspark.sql import functions as F

result = (
    spark.table("customers")
    .select(F.col("name"))
    .orderBy(F.col("name").asc())
    .limit(3)
)
```
<!-- fmt: on -->

| Option | Meaning |
|---|---|
| `-d`, `--dialect` | The SQL dialect: `tsql`, `postgres`, `mysql`, `snowflake`, `bigquery`, `oracle`, or `sas`. Omit it for generic SQL. A `.sas` file is always a SAS program; in a directory, `--dialect sas` selects only the `.sas` files |
| `-o`, `--output` | The file to write, or for a directory input, the directory to write `.py` files into (required) |
| `--notebook` | Write Databricks notebooks instead of plain code; see [databricks.md](databricks.md) |
| `--pipeline` | Write pipeline source files for Lakeflow pipelines instead of plain code; see [pipelines.md](pipelines.md). In a directory, files that define a dataset of the same name are not written |
| `--catalog`, `--schema` | With `--notebook`: the initial values of the notebooks' `catalog` and `schema` widgets, where tables are read from. With `--pipeline`: the schema (in the catalog, if given) that tables without a schema are read from |
| `--output-catalog`, `--output-schema` | With `--notebook`: the initial values of the `output_catalog` and `output_schema` widgets, where results are written; nothing is written without an output schema. See [databricks.md](databricks.md#writing-results-to-tables) |
| `--strict` | Reject any conversion with warnings and write no output for it |

The code goes to standard output and every message to standard error, so
`> query.py` never captures an error. For a directory, each `.sql` or `.sas` file,
including those in subdirectories, becomes a `.py` file at the same relative
path under the output directory; files that cannot be converted are listed
with their issues, followed by a summary such as
`converted 8 of 10 files; 2 could not be converted`.

Before converting a directory, SparkShift checks the output paths of all selected
inputs. If two inputs would share a `.py` path (for example, `job.sql` and
`job.sas`), or one output file would also need to be another output's directory,
the entire batch stops with exit status 2 before conversion or writing. The error
lists every conflicting input and destination. Names are compared ignoring case,
even on Linux, so batches remain portable to Windows. Rename the inputs or put
them in separate subdirectories; `sql/job.sql` and `sas/job.sas` are allowed.
`--dialect sas` checks only the selected `.sas` files. This preflight checks names
within the batch; ordinary conversion or filesystem errors can still leave a
partially completed batch.

### Strict mode

Use `sparkshift convert job.sas --strict -o job.py` to reject **any** warning,
including warnings about possible result differences. Warnings still go to
standard error, but the rejected input produces no code or notebook and exits
with status 1. This works with files, standard input, directories, and
`--notebook`. Without `--strict`, conversion keeps its existing behavior: code
is emitted with warnings for review.

In a strict batch, warning-free files are written and rejected files count as
failures in the summary. Existing outputs for rejected files are left untouched;
use a fresh output directory and check the exit status before using batch output.
Use `-o` to preserve existing files on rejection: shell redirection (`> job.py`)
can truncate a file before SparkShift starts. Strict mode rejects all warnings,
including informational ones; a warning-free conversion is not a proof of
correctness for arbitrary data. The Python API and `report` retain their existing
warning data and reporting behavior.

## report

Before a migration, `sparkshift report` tells you how much of a set of SQL
files converts and which constructs block the rest, without writing any code:

```bash
sparkshift report queries/ --dialect snowflake          # summary for the terminal
sparkshift report queries/ -f markdown -o coverage.md   # tables for a pull request or wiki
sparkshift report queries/ -f json                      # for other tools
sparkshift report queries/ --fail-under 90              # exit 1 below 90% converted, for CI
```

For example, on the small mixed workload in
[examples/coverage](../examples/coverage):

```bash
sparkshift report examples/coverage
```

<!-- Exact SparkShift output, checked by tests/test_readme.py. -->
```text
SparkShift coverage report: examples/coverage (generic SQL)

Files            7
Converted        2  28.6%
  with warnings  0   0.0%
Not converted    4  57.1%
Invalid SQL      1  14.3%
Unreadable       0   0.0%

What blocks conversion
Construct                       Files  Occurrences
function MY_UDF                     2            2
OFFSET clause                       2            2
several statements in one file      1            1

Invalid SQL
  customers/unfinished.sql: Incomplete WHERE near 'WHERE' (line 3, column 1)

Hints and example files: --format markdown or --format json.
```

Every file gets exactly one status: converted, converted with warnings, not
converted, invalid SQL, or unreadable. Blockers are ranked by the number of
files they block, then by how often they occur, so the first rows are the
rewrites that unblock the most files. The Markdown and JSON reports add each
blocker's hint, an example file, and every file's status. The JSON has a
`version` field; its status counts are separate and add up to `files`, and
`converted_percent` includes the files converted with warnings.

## upload

`sparkshift upload PATH --to FOLDER` converts a file, or every `.sql` and
`.sas` file in a directory, into notebooks and imports them into a Databricks
workspace folder. It takes `--dialect`, `--catalog`, `--schema`,
`--output-catalog`, `--output-schema`, and `--strict` as `convert` does,
plus `--job [NAME]` (also create a job that runs the notebooks in order; see
[databricks.md](databricks.md#a-job-that-runs-the-notebooks)), `--pipeline
[NAME]` (upload pipeline source files instead, and create a Lakeflow pipeline
from them, keeping its tables in `--output-catalog` and `--output-schema`,
both required; see [pipelines.md](pipelines.md#creating-the-pipeline)),
`--profile`, `--overwrite`, and
`--dry-run`. It needs `pip install 'sparkshift[databricks]'`, except with
`--dry-run`. See [databricks.md](databricks.md#uploading-to-a-workspace).

## Exit status

| Status | `convert` | `report` | `upload` |
|---|---|---|---|
| 0 | Every input was converted | The report was produced, whatever it finds | Every input was converted and uploaded |
| 1 | Some input could not be converted, or `--strict` rejected its warnings | `--fail-under` was given and fewer files convert | Some input could not be converted or uploaded, or the `--job` or `--pipeline` could not be created |
| 2 | Usage error, such as a missing file, an unknown dialect, or colliding output paths | Usage error | Usage error, or no connection to Databricks |
