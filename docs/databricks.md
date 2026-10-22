# Databricks

SparkShift writes generated code as Databricks notebooks, imports them into a
workspace, and has been validated on Databricks serverless compute.

## Notebooks

`--notebook` writes a Databricks notebook instead of plain code, for SQL and
SAS, one file or a whole directory:

```bash
sparkshift convert jobs/ --notebook -o notebooks/ --catalog main --schema sales
```

Each notebook is a `.py` file in Databricks' source format: a Markdown cell
that names the source file, says that the code needs a PySpark 4 client
(serverless environment version 4 or later), and lists the warnings to
review; a cell with `catalog`, `schema`, `output_catalog`, and
`output_schema` widgets; a cell for each part of the code (imports, each
DataFrame, the result); a cell that writes the results; and
`display(result)`.
Import it into a workspace (**Workspace → Import**) and run it; `spark`,
`dbutils`, and `display` are defined there.

Tables are read by name from the catalog and schema in the widgets, which
appear as text boxes at the top of the notebook. `--catalog` and `--schema`
give their initial values; an empty box keeps the current catalog or schema
(by default, both start empty). To read other tables, type another schema
and run the notebook again, or pass `catalog` and `schema` as parameters of
a job. In Python, `sparkshift.to_notebook(result, source="job.sql",
catalog="main", schema="sales")` does the same. See
[ADR 0007](adr/0007-databricks-notebooks.md).

### Writing results to tables

A notebook can also save what it computes, so that a scheduled job produces
tables rather than only a display:

```bash
sparkshift convert jobs/ --notebook -o notebooks/ --schema raw --output-schema claims
```

When the `output_schema` widget is set, the notebook writes its results as
tables in that schema, in the catalog in `output_catalog` (or the current
catalog when it is empty), replacing any table of the same name, columns
included. While `output_schema` is empty, nothing is written, so opening a
notebook to look at it never changes a table.

- A SAS program writes **each data set it creates**, under its own name, as
  SAS keeps every data set of a program; if it creates one name twice, the
  second is written, as SAS replaces the first.
- A SQL query writes its **result**, as a table named after its file:
  `daily_revenue.sql` becomes `daily_revenue` (lowercase, with characters
  other than letters, digits, and underscores replaced by `_`).

The Markdown cell at the top names every table the notebook can write. The
names are quoted as identifiers, never put into SQL text, and the widgets are
also job parameters. To run notebooks one after another, where one reads a
table another wrote, read from the schema you write to: for SAS programs,
whose data sets are temporary `WORK` data sets, that schema plays the part of
`WORK`, while `libref.member` names still read `libref` as a schema.

## Uploading to a workspace

`sparkshift upload` converts files into notebooks and imports them into a
workspace folder, keeping their relative paths (`jobs/daily/orders.sql`
becomes `FOLDER/daily/orders`). It needs the optional Databricks SDK:

```bash
pip install 'sparkshift[databricks]'
export DATABRICKS_HOST=https://<your-workspace>.cloud.databricks.com
export DATABRICKS_TOKEN=...   # a personal access token, set in your own shell
sparkshift upload jobs/ --to /Workspace/Users/you@example.com/migrated --dry-run
sparkshift upload jobs/ --to /Workspace/Users/you@example.com/migrated
```

- Credentials come only from the Databricks SDK's usual sources: the
  environment variables above, or a profile in `~/.databrickscfg`
  (`--profile NAME`). There is no option to pass a token, and SparkShift
  replaces any secret it knows of with `***` in its messages.
  [`.env.example`](../.env.example) lists the variables.
- `--dry-run` converts and lists the notebook paths without connecting (and
  works without the extra).
- `--catalog`, `--schema`, `--output-catalog`, and `--output-schema` set the
  notebooks' widgets, as for `convert --notebook`, so the notebooks read and
  write the right tables as uploaded.
- An existing notebook is never replaced unless you pass `--overwrite`; it is
  reported, and the other files are still uploaded. `--strict` skips any
  file whose conversion has warnings. The exit status is 1 if any file
  could not be converted or uploaded.

See [ADR 0010](adr/0010-upload-notebooks-with-the-databricks-sdk.md).

### A job that runs the notebooks

`--job` also creates a Databricks job that runs the uploaded notebooks one
after another, in the order of their paths, on serverless compute in
environment version 4:

```bash
sparkshift upload claims/ --to /Workspace/Users/you@example.com/claims --job \
    --schema raw --output-schema claims
```

- The job is named `sparkshift` and the folder's name (`sparkshift claims`),
  or `--job NAME`. It appears under **Jobs & Pipelines**, where you can run
  it or give it a schedule.
- The notebooks' widgets are the job's parameters, starting at the values of
  `--catalog`, `--schema`, `--output-catalog`, and `--output-schema`; a run
  can change them. Runs do not overlap, as they write tables.
- The job is created only if every file converts and uploads: a job missing
  one step of a chain would compute something else without saying so.
- If a job of that name exists, it is kept and reported, unless you pass
  `--overwrite`, which updates it. If several jobs have the name, none is
  changed.
- With `--dry-run`, the order of the job's tasks is printed instead.

The job is described once, in `sparkshift.jobs`. The playground cannot
create jobs itself, because Databricks does not let web pages from other
sites call the Jobs API, so it imports a **Create job** notebook with the
same description: run it once in the workspace and it creates the job, or
updates the one of the same name. See
[ADR 0012](adr/0012-create-jobs-from-one-description.md).

## Pipelines

`--pipeline` writes source files for Lakeflow pipelines instead of notebooks,
and `upload --pipeline` creates the pipeline: each data set, or a query's
result, becomes a materialized view, and the pipeline orders them by what
they read. See [pipelines.md](pipelines.md).

## Running on Databricks

The generated code uses PySpark 4 APIs, such as `Column.outer()` for
correlated subqueries, so it needs a PySpark 4 client. On serverless compute
that is environment version 4 or later; environment versions 2 and 3 have a
PySpark 3.5 client even though the server runs Spark 4. Every example in
`examples/sql` and `examples/sas` has been run on a Databricks Free Edition
workspace in serverless environments 4 and 5 and returned the same column
types and rows as on local Spark; see
[databricks-validation.md](databricks-validation.md).
