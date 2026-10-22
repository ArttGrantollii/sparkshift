# Validation on Databricks

The equivalence tests run on a local Apache Spark, in classic and Spark
Connect modes (see [testing.md](testing.md)). This page records runs of the
same generated code on a real Databricks workspace.

## How it is checked

[`tools/validate_databricks.py`](../tools/validate_databricks.py) is run by
hand, with a `~/.databrickscfg` profile; CI never runs it, because it needs
an account's credentials. It:

1. converts every example in [`examples/`](../examples/) and runs the code on
   local Spark against the test tables. Those results are the expected ones:
   the equivalence tests check them against the original SQL, and the SAS
   examples against rows derived by hand from SAS's documentation;
2. creates the same tables, with the same column types and rows, in the
   schema `workspace.sparkshift`;
3. uploads the examples as notebooks with `sparkshift upload --catalog
   workspace --schema sparkshift`, plus a driver notebook;
4. runs the driver once as a serverless job. For each example, it runs the
   example's notebook with `%run`; the notebook's own `catalog` and `schema`
   widgets select the tables (the driver runs no `USE` statement). It then
   compares `result` with the expected
   column types (`DataFrame.schema.simpleString()`) and rows, as a multiset:
   decimals exactly, dates as ISO text, and doubles to 10 significant
   digits. Row order is checked by the local tests.

```bash
uv run python tools/validate_databricks.py --profile sparkshift \
    --folder /Workspace/Users/you@example.com/sparkshift-validation \
    --environment 4
```

It prints one line per example and exits with 1 if any differ.

With `--write-schema CATALOG.SCHEMA`, the notebooks are uploaded with
`--output-catalog` and `--output-schema` as well, so each also writes its
results to tables there. Before running them, the driver creates every one of
those tables with other columns, so each write must replace a table, schema
included; after each example it compares every table the example wrote with
the expected types and rows of that table.

With `--pipeline CATALOG.SCHEMA`, it validates a Lakeflow pipeline instead
([pipelines](pipelines.md)). It creates the tables as above, converts every
example into a pipeline source file, with one more SAS program whose data
sets use `drop`, `distinct`, and `dropDuplicates` on each other, uploads the
files, and creates or updates one pipeline, `sparkshift validation`, that
keeps its tables in `CATALOG.SCHEMA`. It runs the pipeline twice, a full
refresh and then an update, because a later run is where Apache Spark's own
pipelines can go wrong ([ADR 0013](adr/0013-pipelines-from-the-same-plan.md)),
and after each run compares every table the pipeline keeps with what the
plain code computes on local Spark.

```bash
uv run python tools/validate_databricks.py --profile sparkshift \
    --folder /Workspace/Users/you@example.com/sparkshift-validation \
    --pipeline workspace.sparkshift_pipeline
```

## Results

### 2026-10-15, Databricks Free Edition, serverless compute

| Serverless environment | Spark (server) | PySpark (client) | Examples matching local Spark |
|---|---|---|---|
| 4 | 4.2.0 | 4.0.0 | 11 of 11 |
| 5 | 4.2.0 | 4.1.0 | 11 of 11 |

The examples are the 8 SQL queries in `examples/sql` (generic SQL,
BigQuery, MySQL, Oracle, PostgreSQL, Snowflake, and T-SQL) and the 3 SAS
programs in `examples/sas`.

A later run on the same day, after the notebooks gained their `catalog` and
`schema` widgets, matched 11 of 11 in environment 4 with the driver's `USE`
statements removed: the widgets alone selected the schema.

To check that the comparison can fail, one run had two expectations changed
on purpose: a row removed from one example and a column type changed in
another. It reported exactly those two examples as different, with the extra
row and both types.

### 2026-10-17, writing results to tables

With `--write-schema workspace.sparkshift_results`, in serverless environment
4 (Spark 4.2.0, PySpark 4.0.0): **23 of 23** matched local Spark, the 11
examples' results and the 12 tables their notebooks wrote: one per SQL query,
named after its file, and one per SAS data set, both data sets for
`customer_ratings.sas` (`rated` and `ranked`). Every table existed before the
run with other columns, so every write replaced a Unity Catalog table and its
schema.

### A PySpark 4 client is needed

A job submitted without an environment ran in serverless environment 2,
with PySpark 3.5.0 as the client although the server ran Spark 4.2.0. The
example with a correlated `NOT EXISTS` failed there:

```
F.col("c.customer_id").outer()
TypeError: 'Column' object is not callable
```

`Column.outer()` and `DataFrame.scalar()`, which SparkShift generates for
correlated and scalar subqueries, were added in PySpark 4.0. Measured
clients:

| Serverless environment | PySpark (client) | `Column.outer`, `DataFrame.scalar` |
|---|---|---|
| 2 (a job's default here) | 3.5.0 | missing |
| 3 | 3.5.2 | missing |
| 4 | 4.0.0 | present |
| 5 | 4.1.0 | present |

So on Databricks serverless compute, run generated code in environment
version 4 or later; the validation tool sets it with `--environment`, and
every generated notebook says so in its first cell.
