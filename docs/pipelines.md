# Lakeflow pipelines

SparkShift can write converted SQL and SAS as source files for Databricks'
Lakeflow pipelines (formerly Delta Live Tables), and create the pipeline.
Where a job runs notebooks one after another
([Databricks](databricks.md#a-job-that-runs-the-notebooks)), a pipeline defines **datasets**: each is
a function that returns a DataFrame, and the pipeline works out the order to
compute them from the tables each one reads. See
[ADR 0013](adr/0013-pipelines-from-the-same-plan.md) for the design.

## Converting

```bash
sparkshift convert jobs/ --pipeline -o pipeline/ --catalog main --schema raw
```

Each `.sql` or `.sas` file becomes a `.py` pipeline source file:

<!-- fmt: off -->
```python
# Pipeline source converted from account_risk.sas (SAS) by SparkShift 0.2.0.
# ...

from pyspark import pipelines as dp
from pyspark.sql import functions as F


@dp.materialized_view(name="transaction_summary")
def transaction_summary():
    accounts = spark.table("accounts")
    transactions = spark.table("transactions")

    return (
        accounts.alias("a")
        .join(transactions.alias("t"), ...)
        .groupBy(...)
        .agg(...)
    )
```
<!-- fmt: on -->

- Each **data set a SAS program creates** is a materialized view of the same
  name, as the table a notebook would write. When a program creates a name
  again, as PROC SORT without `OUT=` does, the versions it replaces are
  temporary views, `name_v1`, `name_v2`, which only the pipeline reads.
- A **SQL query** is a materialized view named after its file:
  `daily_revenue.sql` becomes `daily_revenue`.
- Datasets read each other by name, `spark.table("accounts")`, and the
  pipeline resolves those names to its own datasets. That holds across files
  too: one file can read a dataset another defines.
- Other tables are read from `--schema` (in `--catalog`, if given), which
  qualify every table the code names without a schema; without them, they
  are read from the pipeline's own catalog and schema. A pipeline has no
  widgets, so to read other tables, convert again.
- Each file starts with a comment that names its datasets and lists the
  warnings to review. The code in each function is what the plain code
  computes for that data set, except that rows written in a SAS program
  (data lines) have their schema as `T.StructType([...])`: a pipeline may not
  ask Spark to parse a schema written as text while it defines a dataset.

In Python: `sparkshift.to_pipeline(result, source="account_risk.sas",
dialect="sas", catalog="main", schema="raw")`.

### What a pipeline cannot run

These are rejected, with the reason and what to change:

- **A program that creates no data set**: PROC PRINT only shows one, so a
  pipeline would have nothing to keep.
- **A dataset named like a table it reads without a schema**: in a pipeline,
  `orders` would be the pipeline's own `orders`. Read the table with
  `--schema`, or rename the data set.
- **Helper columns named when the code runs**: when SparkShift cannot see the
  input's columns, plain code picks a free name for a helper column by
  looking at them ([ADR 0008](adr/0008-protect-generated-helper-column-names.md)),
  and a pipeline may not do that. In SAS, list the SET data set's columns
  with `KEEP=`; in SQL, select columns by name instead of `*`.
- **Two files that define the same dataset**, in a folder: a pipeline defines
  each dataset once. SQL files are named after their files, so two
  `daily.sql` files in different folders collide; rename one.

## Creating the pipeline

```bash
pip install "sparkshift[databricks]"
sparkshift upload jobs/ --to /Workspace/Users/you@example.com/migrated --pipeline \
    --schema raw --output-catalog main --output-schema claims
```

`upload --pipeline` converts every file into a pipeline source file, imports
them into the workspace folder as files (with their relative paths and a
`.py` suffix), and creates a pipeline named `sparkshift migrated` (or the
name after `--pipeline`) that:

- runs on serverless compute, when started (not continuously);
- keeps its materialized views in `--output-catalog`.`--output-schema`,
  both required;
- has the uploaded files as its source code, and the folder as its root.

The pipeline is created only if every file converts and uploads: a pipeline
missing a file would compute something else without saying so. A pipeline
of the same name is kept and reported unless `--overwrite` is given, which
updates it (and replaces the files). `--dry-run` lists the files and the
pipeline without connecting. Credentials come only from the Databricks SDK's
environment variables or profiles, as for notebooks.

The pipeline then appears under **Jobs & Pipelines**: start it there, or give
it a schedule. Databricks Free Edition allows one active pipeline of each
kind.

## In the playground

The [playground](https://arttgrantollii.github.io/sparkshift/) shows a
program as its pipeline source file (**Pipeline**, next to PySpark and
Notebook), converts a folder into them, and deploys them. In the **Deploy**
tab, choose **A Lakeflow pipeline of the converted files** and type where
the pipeline keeps its tables under **Write results to**, as
`catalog.schema`. The page imports each file into the workspace folder,
with a **Create pipeline** notebook: run it once (**Run all**) to create the
pipeline that `upload --pipeline` would create, or to update the one of the
same name. The notebook calls the Pipelines API from inside the workspace,
because a web page from another site may not
([playground](../playground/README.md#deploying-to-databricks)).

## Apache Spark's pipelines

Apache Spark 4.1 and later run the same files with `spark-pipelines`, but not
safely yet, so SparkShift does not support them: on a dataset of the same
pipeline, `drop`, `distinct`, and `dropDuplicates` fail, and `withColumn`
can make a later run compute a dataset before its input
([SPARK-59663](https://issues.apache.org/jira/browse/SPARK-59663)). The test
suite still uses them on a new schema, where the first run is right, to
check that each materialized view holds what the plain code computes
([testing](testing.md)).
