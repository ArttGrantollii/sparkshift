# ADR 0007: Databricks notebooks in source format

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Phase 5 brings SparkShift's output to Databricks. The generated code already
runs there unchanged: it uses only the DataFrame API and expects a
SparkSession named `spark`, which Databricks notebooks define. What is
missing is a file Databricks opens as a notebook.

Databricks imports notebooks in several formats (Databricks documentation,
"Import and export Databricks notebooks"): a source file (`.py`), Jupyter
(`.ipynb`), HTML, and the DBC archive. A Python source-format notebook is a
`.py` file that starts with `# Databricks notebook source`, with
`# COMMAND ----------` between cells and `# MAGIC %md` lines for Markdown.

A check in a Databricks Free Edition workspace (2026-10-12) showed serverless
compute on Spark 4.2.0, the version SparkShift is tested with, and
`workspace.default` as the current catalog and schema.

## Decision

- **Source format, not Jupyter.** A source notebook is plain Python: it reads
  well in a diff and a code review, Python tools can check it, and it imports
  into Databricks as a notebook. Jupyter's JSON would hide the code in strings.
- **One cell per section of the generated code**: the imports, the table and
  window variables, each DataFrame, and `result`, so each data set can be run
  and inspected on its own. The cells together are the same program as the
  plain code.
- **A Markdown cell first**, naming the source file, its language, and the
  SparkShift version, with the warnings to review. A notebook travels without
  the command line that printed those warnings.
- **`display(result)` last**, to show the result as Databricks notebooks do.
- **Tables keep the names in the source.** The notebook says that they are
  read from the current catalog and schema. Rewriting names for Unity
  Catalog's three-level names would be a guess about the user's catalog.
- `sparkshift convert --notebook` and `sparkshift.to_notebook(...)` write it,
  for SQL and SAS alike.

## Consequences

- Notebooks are verified like the plain code: for every example, running the
  notebook on Spark displays the same rows as running the code.
- Uploading notebooks to a workspace and running them there are separate
  steps (later milestones of Phase 5), so writing a notebook needs no
  Databricks account or credentials.
