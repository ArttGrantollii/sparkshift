# ADR 0013: pipelines from the same plan, for Databricks

- **Status:** Accepted
- **Date:** 2026-10-22

## Context

Notebooks and jobs (ADR 0007, ADR 0012) run converted code as a script:
each step after the one before. Databricks' Lakeflow pipelines, formerly
Delta Live Tables, run datasets instead: a source file defines each dataset
as a function that returns a DataFrame, and the pipeline works out the order
from the tables each function reads. Their Python API is `pyspark.pipelines`,
also the API of Apache Spark's own Spark Declarative Pipelines (Spark 4.1 and
later), which run through Spark Connect.

Measured (2026-10-22), on Apache Spark 4.2 locally:

- A pipeline may not analyze anything while a function defines its dataset
  (`ATTEMPT_ANALYSIS_IN_PIPELINE_QUERY_FUNCTION`). `df.columns` analyzes, and
  so does `createDataFrame` with a schema written as text, which Spark
  Connect sends to the server to parse.
- On a dataset of the same pipeline, `drop`, `distinct`, `dropDuplicates`,
  and `na.drop` fail when the pipeline is defined (`TABLE_OR_VIEW_NOT_FOUND`):
  Spark Connect plans them by analyzing their input, which does not exist
  yet. `select`, `where`, `withColumn`, joins, aggregation, windows, set
  operations, and subqueries are accepted.
- [SPARK-59663](https://issues.apache.org/jira/browse/SPARK-59663) (open,
  4.1.0 to 4.2.0): `withColumn` on a dataset of the same pipeline is analyzed
  as it is defined; once that dataset's table exists, which is from the
  second run on, the analysis succeeds and the pipeline loses the dependency,
  so it can compute a dataset before its input. The first run is right.

Databricks Free Edition allows one active pipeline of each kind. Browsers may
not call the Pipelines API, as for the Jobs API (ADR 0012).

## Decision

- **One pipeline source file per input, from the same plan.**
  `sparkshift.to_pipeline(result)` uses the IR the plain code was emitted
  from (kept on the result, privately), and the emitter writes a function
  for each dataset: inside, the code is what the plain output computes for
  it, ending in `return`. Python, not SQL: SparkShift's semantics (SAS's
  missing values, Spark's expressions) are written in PySpark.
- **The datasets are the tables a notebook writes.** Each data set a SAS
  program creates is a materialized view of the same name; when a program
  creates a name again (PROC SORT without OUT=), the versions it replaces
  are temporary views, `name_v1`, `name_v2`, which only the pipeline reads.
  A SQL query is a materialized view named after its file. PROC PRINT only
  shows, so it is not part of a pipeline; a program that creates no data
  set is rejected.
- **Datasets read each other by name**, `spark.table("name")`, which the
  pipeline resolves to its own datasets and uses to order them. Other tables
  are read from `--catalog` and `--schema`, given when converting, because
  a pipeline has no widgets to change them; without them, from the
  pipeline's own catalog and schema. A program that defines a dataset with
  the name of a table it reads without a schema is rejected: in a pipeline,
  that name is the dataset itself. In a folder, a dataset may be defined
  once.
- **Nothing is analyzed while a dataset is defined.** Data lines' schemas
  are written as `T.StructType([...])`, not as text. Helper columns named
  when the code runs (ADR 0008) look at the input's columns, so a pipeline
  rejects them, with how to avoid them: KEEP= on SET in SAS, named columns
  instead of `*` in SQL.
- **Databricks is the target; Apache Spark's pipelines are not supported**:
  with SPARK-59663, a second run can be silently wrong, and the steps above
  fail. Generated files keep the usual steps (`drop`, `distinct`,
  `dropDuplicates`), which Databricks must run on every run: its validation
  runs the pipeline twice.
- **Tested both ways.** Locally, the test suite runs pipelines on Apache
  Spark, each in a new schema, where the first run is right, for programs
  without the failing steps, and compares every materialized view with what
  the plain code computes. On Databricks, `tools/validate_databricks.py
  --pipeline` runs every example, and a program with those steps, as one
  pipeline, twice, and compares every table after each run.
- **Deployed as jobs are (ADR 0012).** `sparkshift.pipelines.pipeline_settings`
  is the Pipelines API's `create` body: serverless, triggered, its tables in
  a catalog and schema of Unity Catalog, its source files as workspace files.
  `upload --pipeline` sends it; `create_pipeline_notebook` writes a notebook
  that sends it from inside the workspace, for the playground. Pipelines are
  created only when every file converts and uploads, and names are never
  guessed: an existing pipeline is updated only with `--overwrite`.

## Alternatives

- **SQL pipeline files** (`CREATE MATERIALIZED VIEW`): SparkShift would need
  a SQL emitter, and SAS semantics are written as PySpark expressions.
- **The `dlt` module**: Databricks' former name for the same API;
  `pyspark.pipelines` is current and the same in Apache Spark.
- **Inlining replaced versions** into the data set that reads them: the code
  repeats when several data sets read one version.
- **Rewriting `drop` and `distinct`** into steps Apache Spark accepts (for
  example `selectExpr("* EXCEPT (x)")`): it would not fix SPARK-59663, which
  concerns `withColumn`, the step most generated code uses.

## Consequences

- One plan gives plain code, notebooks, jobs, and pipelines; a translation
  fix reaches all four.
- A pipeline computes its datasets in the order of their reads, not of the
  files: SQL files that do not read each other's results run independently.
- Helper columns named at run time, and programs that only print, have no
  pipeline form yet.
