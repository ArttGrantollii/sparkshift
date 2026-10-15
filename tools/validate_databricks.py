"""Run the gallery examples on a Databricks workspace and compare their results
with local Spark (Phase 5.4).

Usage, from the repository root, with a ~/.databrickscfg profile:

    uv run python tools/validate_databricks.py --profile sparkshift \\
        --folder /Workspace/Users/you@example.com/sparkshift-validation

Locally, it converts every example in examples/sql and examples/sas and runs
the code on Spark against the test tables (tests/equivalence/datasets.py);
the equivalence tests check those results against the original SQL and the
hand-derived SAS rows. On the workspace, it:

1. creates or replaces the schema's tables (default workspace.sparkshift)
   with the same rows and column types;
2. uploads the examples with `sparkshift upload --catalog --schema` under
   FOLDER/examples, and a driver notebook at FOLDER/driver;
3. runs the driver once on serverless compute. The driver runs each example
   notebook with %run, whose widgets select the schema, and compares its
   `result` with the local column types and rows (as a multiset; order is
   checked locally).

It prints one line per example and exits with 1 if any differ. Credentials
come only from the profile; nothing here prints them.
"""

import argparse
import datetime
import inspect
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
EXAMPLES = REPOSITORY / "examples"
sys.path.insert(0, str(REPOSITORY / "tests" / "equivalence"))


def canonical(value: Any) -> Any:
    """A value as JSON, the same for local Spark and Databricks: decimals and
    other values as text, dates in ISO format, doubles to 10 digits."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return format(value, ".10g")
    if isinstance(value, datetime.date):  # also datetime.datetime
        return value.isoformat()
    return str(value)


def rows_of(frame: Any) -> list[str]:
    """A DataFrame's rows, canonical and sorted: a multiset."""
    return sorted(json.dumps([canonical(v) for v in row]) for row in frame.collect())


def examples() -> list[tuple[str, Path, str | None]]:
    """(name, file, dialect) of every example: sql/<dialect>/*.sql and sas/."""
    found = []
    for folder in sorted((EXAMPLES / "sql").iterdir()):
        dialect = None if folder.name == "generic" else folder.name
        for path in sorted(folder.glob("*.sql")):
            found.append((f"sql/{folder.name}/{path.stem}", path, dialect))
    for path in sorted((EXAMPLES / "sas").glob("*.sas")):
        found.append((f"sas/{path.stem}", path, "sas"))
    return found


def local_results() -> dict[str, tuple[str, list[str]]]:
    """Each example's column types and rows on local Spark."""
    from datasets import TABLES
    from pyspark.sql import SparkSession

    import sparkshift

    spark = (
        SparkSession.builder.master("local[1]")
        .config("spark.sql.shuffle.partitions", "1")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    try:
        for name, (schema, rows) in TABLES.items():
            spark.createDataFrame(rows, schema).createOrReplaceTempView(name)
        results = {}
        for name, path, dialect in examples():
            code = sparkshift.convert(path.read_text(), dialect=dialect).code
            scope: dict[str, Any] = {"spark": spark}
            exec(code, scope)
            frame = scope["result"]
            results[name] = (frame.schema.simpleString(), rows_of(frame))
        return results
    finally:
        spark.stop()


def driver_source(
    expected: dict[str, tuple[str, list[str]]], catalog: str, schema: str
) -> str:
    """The driver notebook, in Databricks source format."""
    from datasets import TABLES

    tables = {
        name: (
            ", ".join(f"`{f.name}` {f.dataType.simpleString()}" for f in struct),
            rows,
        )
        for name, (struct, rows) in TABLES.items()
    }
    setup = "\n".join(
        [
            "import datetime",
            "import json",
            "from decimal import Decimal",
            "from typing import Any",
            "",
            'spark.conf.set("spark.sql.session.timeZone", "UTC")',
            "",
            inspect.getsource(canonical),
            inspect.getsource(rows_of),
            f"_validation_expected = {expected!r}",
            "_validation_report = []",
            "",
            "",
            "def _validation_check(name, frame):",
            '    """Compare a result with the expected types and rows."""',
            "    types, rows = _validation_expected[name]",
            "    actual = rows_of(frame)",
            "    entry = {'example': name, 'status': 'pass'}",
            "    if frame.schema.simpleString() != types:",
            "        entry['status'] = 'fail'",
            "        entry['types'] = [types, frame.schema.simpleString()]",
            "    missing = [r for r in rows if r not in actual]",
            "    unexpected = [r for r in actual if r not in rows]",
            "    if missing or unexpected or len(rows) != len(actual):",
            "        entry['status'] = 'fail'",
            "        entry['missing'] = missing[:10]",
            "        entry['unexpected'] = unexpected[:10]",
            "        entry['counts'] = [len(rows), len(actual)]",
            "    _validation_report.append(entry)",
        ]
    )
    load = "\n".join(
        [
            f'spark.sql("CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")',
            f"_validation_tables = {tables!r}",
            "for _name, (_ddl, _rows) in _validation_tables.items():",
            "    (",
            "        spark.createDataFrame(_rows, _ddl)",
            '        .write.mode("overwrite")',
            '        .option("overwriteSchema", "true")',
            f'        .saveAsTable("{catalog}.{schema}." + _name)',
            "    )",
            # No USE statements: each example's widgets select the schema.
        ]
    )
    cells = [setup, load]
    for name, _, _ in examples():
        cells.append(f"# MAGIC %run ./examples/{name}")
        cells.append(f"_validation_check({name!r}, result)")
    cells.append(
        "import pyspark\n"
        "dbutils.notebook.exit(json.dumps({'spark_version': spark.version, "
        "'pyspark_client': pyspark.__version__, 'results': _validation_report}))"
    )
    body = "\n\n# COMMAND ----------\n\n".join(cells)
    return f"# Databricks notebook source\n{body}\n"


def upload_examples(folder: str, profile: str, catalog: str, schema: str) -> None:
    """Upload every example folder with `sparkshift upload`, with notebook
    widgets that select the test tables' catalog and schema."""
    from sparkshift.cli import main

    common = ["--overwrite", "--profile", profile]
    common += ["--catalog", catalog, "--schema", schema]
    for directory in sorted((EXAMPLES / "sql").iterdir()):
        arguments = [
            "upload",
            str(directory),
            "--to",
            f"{folder}/examples/sql/{directory.name}",
        ]
        if directory.name != "generic":
            arguments += ["--dialect", directory.name]
        if main([*arguments, *common]) != 0:
            raise SystemExit(f"uploading {directory} failed")
    sas = ["upload", str(EXAMPLES / "sas"), "--to", f"{folder}/examples/sas"]
    if main([*sas, *common]) != 0:
        raise SystemExit("uploading examples/sas failed")


def run_driver(profile: str, path: str, environment: str) -> dict[str, Any]:
    """Run the driver notebook once on serverless compute, in the given
    serverless environment version; its exit value."""
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.service import compute, jobs

    from sparkshift.databricks import redact

    client = WorkspaceClient(profile=profile)
    secrets = [client.config.token] if client.config.token else []
    waiter = client.jobs.submit(
        run_name=f"sparkshift validation (environment {environment})",
        tasks=[
            jobs.SubmitTask(
                task_key="validate",
                notebook_task=jobs.NotebookTask(notebook_path=path),
                environment_key="serverless",
            )
        ],
        # Without it, a job gets an older default environment, whose PySpark
        # 3.5 client lacks APIs the generated code uses (Column.outer).
        environments=[
            jobs.JobEnvironment(
                environment_key="serverless",
                spec=compute.Environment(environment_version=environment),
            )
        ],
    )
    print(f"run {waiter.run_id} started; waiting for it to finish", file=sys.stderr)
    try:
        waiter.result(timeout=datetime.timedelta(minutes=30))
    except Exception as error:  # the run failed: show the notebook's error
        print(f"run failed: {redact(str(error), secrets)}", file=sys.stderr)
    run = client.jobs.get_run(waiter.run_id)
    task = run.tasks[0]
    output = client.jobs.get_run_output(task.run_id)
    if output.error:
        trace = redact(output.error_trace or "", secrets)
        raise SystemExit(f"{redact(output.error, secrets)}\n{trace}")
    return json.loads(output.notebook_output.result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--folder", required=True, help="absolute workspace folder")
    parser.add_argument("--schema", default="workspace.sparkshift")
    parser.add_argument(
        "--environment",
        default="4",
        help="serverless environment version; 4 is the first with a PySpark 4 "
        "client (default: 4)",
    )
    arguments = parser.parse_args(argv)
    catalog, schema = arguments.schema.split(".")

    print("computing the expected results on local Spark", file=sys.stderr)
    expected = local_results()
    upload_examples(arguments.folder, arguments.profile, catalog, schema)

    from sparkshift.databricks import connect

    workspace = connect(arguments.profile)
    workspace.mkdirs(arguments.folder)
    driver = f"{arguments.folder}/driver"
    workspace.import_notebook(
        driver, driver_source(expected, catalog, schema), overwrite=True
    )
    report = run_driver(arguments.profile, driver, arguments.environment)

    print(
        f"Databricks Spark {report['spark_version']}, serverless environment "
        f"{arguments.environment} (PySpark {report['pyspark_client']})"
    )
    failed = 0
    for entry in report["results"]:
        print(f"{entry['status'].upper():5} {entry['example']}")
        if entry["status"] != "pass":
            failed += 1
            details = {k: v for k, v in entry.items() if k not in ("status", "example")}
            print(json.dumps(details, indent=2, ensure_ascii=False))
    checked = len(report["results"])
    print(f"{checked - failed} of {len(expected)} examples match local Spark")
    return 0 if failed == 0 and checked == len(expected) else 1


if __name__ == "__main__":
    sys.exit(main())
