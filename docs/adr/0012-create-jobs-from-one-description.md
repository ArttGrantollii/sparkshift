# ADR 0012: create jobs from one description, by the CLI or a notebook

- **Status:** Accepted
- **Date:** 2026-10-19

## Context

Converted notebooks only produce tables when something runs them, usually a
scheduled Databricks job. SparkShift reaches a workspace in two ways: the
command line (`sparkshift upload`, through the Databricks SDK) and the
playground, a static page that calls the REST API from the visitor's browser.

A check on a Databricks Free Edition workspace (2026-10-16) found that the
workspace import API answers browsers from other sites (CORS), but the Jobs
API does not: `jobs/create`, `runs/submit`, and `run-now` are refused. A web
page cannot create a job; anything in the workspace can.

## Decision

- **One description of the job**: `sparkshift.jobs.job_settings` returns the
  JSON body of the Jobs API's `create` request. The command line sends it
  (`upload --job`); for the playground, `create_job_notebook` writes it into a
  notebook that sends the same body when run in the workspace, with the
  notebook's own credentials. Both use the raw REST body, so they cannot
  drift apart.
- **One task per notebook, each after the one before**, in the order of
  their paths. Notebooks converted from one folder often feed each other
  (a SAS job's later steps read what earlier ones wrote), and the order of
  the files is the only order SparkShift knows.
- **Serverless compute, environment version 4**, the first with a PySpark 4
  client ([databricks-validation.md](../databricks-validation.md)).
- **The widgets are job parameters** (`catalog`, `schema`, `output_catalog`,
  `output_schema`), so a run can read or write other tables.
- **One run at a time** (`max_concurrent_runs: 1`): runs replace tables.
- **All or nothing**: `upload --job` creates the job only if every file
  converts and uploads. A job missing a step would compute something else
  without saying so.
- **Names are not guessed**: a job of the same name is kept and reported
  unless `--overwrite` is given, which updates it with `jobs/reset`; if
  several jobs share the name, none is changed. Job names are matched
  exactly, though the API's filter ignores case. The notebook, run on purpose
  by the user, always updates the job of its name.

## Consequences

- Validated on the Free Edition workspace (2026-10-19, serverless
  environment 4): `upload --job` created a job for the three SAS examples;
  its run succeeded, its tasks started in order, and it rewrote all four of
  their tables. The Create job notebook, run twice, left exactly one job
  (created, then updated); that job's run succeeded the same way. A job with
  empty parameter defaults is accepted.
- The playground still needs a click in the workspace to create the job; it
  cannot do it alone without a server, which it does not have
  ([ADR 0004](0004-run-the-playground-in-the-browser.md)).
- Tasks in a chain run one after another even when they are independent,
  as SQL queries usually are: slower, but never in the wrong order.
