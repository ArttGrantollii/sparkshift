# SparkShift

[![CI](https://github.com/ArttGrantollii/sparkshift/actions/workflows/ci.yml/badge.svg)](https://github.com/ArttGrantollii/sparkshift/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sparkshift.svg)](https://pypi.org/project/sparkshift/)
[![Python](https://img.shields.io/pypi/pyversions/sparkshift.svg)](https://pypi.org/project/sparkshift/)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Convert SQL and SAS programs into readable, idiomatic, tested PySpark DataFrame
code.

**[Try it in your browser](https://arttgrantollii.github.io/sparkshift/)**: nothing to install, and your code
never leaves your browser.

> **Status: first release (0.1).** SparkShift converts `SELECT` queries, including
> joins, aggregation, ordering, window functions and `QUALIFY`, CTEs, set
> operations, and subqueries, from generic SQL and six dialects (T-SQL,
> PostgreSQL, MySQL, Snowflake, BigQuery, and Oracle), and a documented
> subset of SAS: DATA steps and `PROC SORT`, `MEANS`, `SUMMARY`, `FREQ`, and
> `SQL`. Everything else is rejected with a clear error. See
> [ROADMAP.md](ROADMAP.md) for what is planned.

## Why SparkShift

- **Readable output.** The code looks like what a data engineer would write
  by hand: one chained DataFrame expression per query, named variables for
  CTEs and SAS data sets, and only the imports it needs.
- **It rejects instead of guessing.** When a construct means something
  different in Spark, or only the table schema would tell what it means,
  SparkShift says so, names the construct, and suggests a rewrite. Every
  issue in a query is listed at once.
- **Proven on real engines.** Every supported construct is tested by running
  the original SQL and the generated PySpark on Apache Spark and comparing
  the results; PostgreSQL and MySQL queries also run on real servers in CI,
  the Spark tests also run through Spark Connect, and the examples have been
  run on Databricks. See [How correctness is verified](#how-correctness-is-verified).

## Install

SparkShift needs Python 3.11 or later:

```bash
pip install sparkshift
```

It depends only on [SQLGlot](https://github.com/tobymao/sqlglot): converting
needs neither Spark nor Java. The generated code runs on PySpark 4.

## Usage

```python
import sparkshift

result = sparkshift.convert(
    "SELECT order_id, amount * 1.10 AS with_tax FROM sales.orders"
)
print(result.code)
```

<!-- Exact SparkShift output, checked by tests/test_readme.py. Not reformatted. -->
<!-- fmt: off -->
```python
from pyspark.sql import functions as F

result = (
    spark.table("sales.orders")
    .select(
        F.col("order_id"),
        (F.col("amount") * F.lit("1.10").cast("decimal(3,2)")).alias("with_tax"),
    )
)
```
<!-- fmt: on -->

The generated code imports what it uses, expects a SparkSession named `spark`
(as in a Databricks notebook), and assigns the final DataFrame to `result`.
Pass `dialect=` for non-generic SQL: `tsql`, `postgres`, `mysql`, `snowflake`,
`bigquery`, or `oracle`.

Queries SparkShift cannot translate safely raise `UnsupportedSQLError`, listing
every unsupported construct:

```text
UnsupportedSQLError: 2 unsupported constructs:
  - function MY_UDF: MY_UDF(score)
  - OFFSET clause: OFFSET 20
```

See the [Python API](docs/api.md) for the result, warnings, and errors.

## Command line

```bash
sparkshift convert query.sql --dialect tsql       # print the PySpark code
sparkshift convert queries/ -o pyspark/           # every .sql and .sas file in a directory
sparkshift report queries/ --dialect snowflake    # how much of a workload converts, and what blocks the rest
sparkshift upload jobs/ --to /Workspace/Users/you@example.com/migrated   # notebooks into Databricks
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

The [command-line guide](docs/cli.md) covers every option, strict mode, the
coverage report, and the exit codes.

## SAS

A `.sas` file is converted as a SAS program:

```sas
data low_scores;
    set customers (keep=customer_id name score);
    where score < 3;
    rename name = customer_name;
run;
```

<!-- Exact SparkShift output, checked by tests/test_readme.py. Not reformatted. -->
<!-- fmt: off -->
```python
from pyspark.sql import functions as F

low_scores = (
    spark.table("customers")
    .select(
        F.col("customer_id"),
        F.col("name"),
        F.col("score"),
    )
    .where(F.col("score").isNull() | (F.col("score") < F.lit(3)))
    .withColumnRenamed("name", "customer_name")
)

result = low_scores
```
<!-- fmt: on -->

In SAS a missing number is smaller than every number, so `score < 3` keeps
rows whose score is missing; in SQL and Spark it would drop them, so the code
says so explicitly. No SAS installation is available to test against: SAS
support follows SAS's documented rules, with expected results written by hand.
[docs/sas.md](docs/sas.md) lists what is supported, each rule with its source,
and the limitations.

## Databricks

```bash
sparkshift convert jobs/ --notebook -o notebooks/ --catalog main --schema sales
pip install "sparkshift[databricks]"
sparkshift upload jobs/ --to /Workspace/Users/you@example.com/migrated --catalog main --schema sales
```

`--notebook` writes Databricks notebooks, with `catalog` and `schema` boxes at
the top that choose where tables are read from; `upload` imports them into a
workspace, with credentials only from the Databricks SDK's environment
variables or profiles. The generated code needs a PySpark 4 client: on
serverless compute, environment version 4 or later. See the
[Databricks guide](docs/databricks.md).

## How correctness is verified

Supported translations are backed by automated equivalence tests that execute
both the original SQL and the generated PySpark against the same datasets on
real Apache Spark, then compare the results with SQL semantics in mind: row
order is ignored unless the query orders it, duplicate rows must match in
number, column names and types must match, and NULLs are compared as values.
The test data deliberately includes duplicates, NULLs, empty strings, and
other edge cases.

For dialects Spark cannot run, the reference is Spark SQL written to mean
the same thing. PostgreSQL and MySQL queries are also run on real PostgreSQL
and MySQL servers in CI and compared with the generated PySpark; T-SQL,
Oracle, Snowflake, and BigQuery are not yet checked against their own
databases.

The Spark tests also run through Spark Connect, the way Databricks serverless
compute runs code, in a separate CI job. See [docs/testing.md](docs/testing.md).
The gallery examples have also been run on a Databricks workspace and matched
local Spark; see [docs/databricks-validation.md](docs/databricks-validation.md).

## Documentation

- [Supported SQL](docs/supported-sql.md) — every construct, dialect difference, and known limitation
- [SAS](docs/sas.md) — the supported subset of SAS, with the documented rules it follows
- [Command line](docs/cli.md) — `convert`, `report`, and `upload`, with every option and exit code
- [Python API](docs/api.md) — `convert`, `to_notebook`, results, warnings, and errors
- [Databricks](docs/databricks.md) — notebooks, uploading, and running on Databricks
- [Examples](examples/README.md) — a realistic query in each dialect, and SAS programs, next to the PySpark generated for them
- [Playground](playground/README.md) — the in-browser converter: limits, privacy, and running it locally
- [Architecture](docs/architecture.md), [testing](docs/testing.md), and [decision records](docs/adr/) — how SparkShift is built and why

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and how to
add a construct, and [SECURITY.md](SECURITY.md) for reporting a
vulnerability and how SparkShift handles code and credentials.

## License

[Apache License 2.0](LICENSE)
