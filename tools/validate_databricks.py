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

With --write-schema CATALOG.SCHEMA, the notebooks also write their results
there (`sparkshift upload --output-catalog --output-schema`): the driver
first fills that schema with tables of the same names and other columns, so
every write must replace a table, and after each example compares every table
it wrote with the rows that table should hold.

With --pipeline CATALOG.SCHEMA, it validates pipeline source files instead
(`sparkshift convert --pipeline`): every example, and PIPELINE_PROGRAM, as
one Lakeflow pipeline on serverless compute, keeping its materialized views
in that schema. It loads the test tables, uploads the files, creates or
updates the pipeline, and runs it twice, a full refresh and then an update,
comparing every materialized view with local Spark after each run: Apache
Spark's own pipelines lose datasets' order on later runs (SPARK-59663).

It prints one line per example and exits with 1 if any differ. Credentials
come only from the profile; nothing here prints them.
"""

import argparse
import datetime
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[1]
EXAMPLES = REPOSITORY / "examples"
sys.path.insert(0, str(REPOSITORY / "tests" / "equivalence"))

# Steps that read the pipeline's own datasets and that Apache Spark's
# pipelines analyze early (docs/adr/0013-pipelines-from-the-same-plan.md):
# drop (DROP), dropDuplicates (NODUPKEY), and distinct (SELECT DISTINCT).
PIPELINE_PROGRAM = (
    "sas/pipeline_steps",
    "data scored; set customers(keep=customer_id country score); "
    "where score ne .; run; "
    "data trimmed; set scored; drop country; run; "
    "proc sort data=scored out=unique_scores nodupkey; by score; run; "
    "proc sql; create table countries as select distinct country from scored; "
    "quit;",
)


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


def local_results(
    extra: tuple[str, str] | None = None,
) -> tuple[
    dict[str, tuple[str, list[str]]], dict[str, dict[str, tuple[str, list[str]]]]
]:
    """Each example's column types and rows on local Spark, and those of each
    table its notebook writes: a SQL query's result, named after its file, or
    each data set of a SAS program (the last of each name). With ``extra``, a
    (name, SAS program) pair, that program's too."""
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
        tables = {}
        sources = [
            (name, path.stem, path.read_text(), dialect)
            for name, path, dialect in examples()
        ]
        if extra:
            sources.append((extra[0], Path(extra[0]).name, extra[1], "sas"))
        for name, stem, text, dialect in sources:
            converted = sparkshift.convert(text, dialect=dialect)
            scope: dict[str, Any] = {"spark": spark}
            exec(converted.code, scope)
            frame = scope["result"]
            results[name] = (frame.schema.simpleString(), rows_of(frame))
            written = {stem.lower(): "result"}
            if converted.datasets:
                written = {d.name: d.variable for d in converted.datasets}
            tables[name] = {
                table: (scope[variable].schema.simpleString(), rows_of(scope[variable]))
                for table, variable in written.items()
            }
        return results, tables
    finally:
        spark.stop()


def driver_source(
    expected: dict[str, tuple[str, list[str]]],
    catalog: str,
    schema: str,
    written: dict[str, dict[str, tuple[str, list[str]]]] | None = None,
    write_schema: str | None = None,
) -> str:
    """The driver notebook, in Databricks source format. With ``written`` and
    ``write_schema``, it also checks the tables each example writes."""
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
            f"_validation_written = {written or {}!r}",
            f"_validation_write_schema = {write_schema!r}",
            "",
            "",
            "def _validation_check_tables(name):",
            '    """Compare each table an example wrote with its expected rows."""',
            "    for table, expected in _validation_written.get(name, {}).items():",
            "        _validation_check(",
            '            f"{name} -> {table}",',
            '            spark.table(f"{_validation_write_schema}.{table}"),',
            "            expected,",
            "        )",
            "",
            "",
            "def _validation_check(name, frame, expected=None):",
            '    """Compare a result with the expected types and rows."""',
            "    types, rows = expected or _validation_expected[name]",
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
    if write_schema:
        # Tables with other columns, so every write must replace one.
        load += "\n".join(
            [
                "",
                f'spark.sql("CREATE SCHEMA IF NOT EXISTS {write_schema}")',
                "for _tables in _validation_written.values():",
                "    for _table in _tables:",
                "        spark.range(1).write.mode('overwrite').option(",
                "            'overwriteSchema', 'true'",
                f"        ).saveAsTable('{write_schema}.' + _table)",
            ]
        )
    cells = [setup, load]
    for name, _, _ in examples():
        cells.append(f"# MAGIC %run ./examples/{name}")
        check = f"_validation_check({name!r}, result)"
        if write_schema:
            check += f"\n_validation_check_tables({name!r})"
        cells.append(check)
    cells.append(
        "import pyspark\n"
        "dbutils.notebook.exit(json.dumps({'spark_version': spark.version, "
        "'pyspark_client': pyspark.__version__, 'results': _validation_report}))"
    )
    body = "\n\n# COMMAND ----------\n\n".join(cells)
    return f"# Databricks notebook source\n{body}\n"


def upload_examples(
    folder: str, profile: str, catalog: str, schema: str, write_schema: str | None
) -> None:
    """Upload every example folder with `sparkshift upload`, with notebook
    widgets that select the test tables' catalog and schema, and the schema
    to write results to, if any."""
    from sparkshift.cli import main

    common = ["--overwrite", "--profile", profile]
    common += ["--catalog", catalog, "--schema", schema]
    if write_schema:
        output_catalog, output_schema = write_schema.split(".")
        common += ["--output-catalog", output_catalog]
        common += ["--output-schema", output_schema]
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


def tables_source(catalog: str, schema: str, *extra_schemas: str) -> str:
    """A notebook that creates or replaces the test tables in
    ``catalog``.``schema``, and creates ``extra_schemas`` (CATALOG.SCHEMA)."""
    from datasets import TABLES

    tables = {
        name: (
            ", ".join(f"`{f.name}` {f.dataType.simpleString()}" for f in struct),
            rows,
        )
        for name, (struct, rows) in TABLES.items()
    }
    lines = [
        "import datetime",
        "import json",
        "from decimal import Decimal",
        "",
        f'spark.sql("CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")',
        *(
            f'spark.sql("CREATE SCHEMA IF NOT EXISTS {extra}")'
            for extra in extra_schemas
        ),
        f"_validation_tables = {tables!r}",
        "for _name, (_ddl, _rows) in _validation_tables.items():",
        "    (",
        "        spark.createDataFrame(_rows, _ddl)",
        '        .write.mode("overwrite")',
        '        .option("overwriteSchema", "true")',
        f'        .saveAsTable("{catalog}.{schema}." + _name)',
        "    )",
        "dbutils.notebook.exit(json.dumps({'tables': sorted(_validation_tables)}))",
    ]
    return "# Databricks notebook source\n" + "\n".join(lines) + "\n"


def check_source(
    expected: dict[str, tuple[str, list[str]]], pipeline_schema: str
) -> str:
    """A notebook that compares each materialized view in ``pipeline_schema``
    with its expected column types and rows (as a multiset)."""
    lines = [
        "import datetime",
        "import json",
        "from typing import Any",
        "",
        'spark.conf.set("spark.sql.session.timeZone", "UTC")',
        "",
        inspect.getsource(canonical),
        inspect.getsource(rows_of),
        f"_expected = {expected!r}",
        "_report = []",
        "for _table, (_types, _rows) in _expected.items():",
        f'    _frame = spark.table("{pipeline_schema}." + _table)',
        "    _actual = rows_of(_frame)",
        "    _entry = {'example': _table, 'status': 'pass'}",
        "    if _frame.schema.simpleString() != _types:",
        "        _entry['status'] = 'fail'",
        "        _entry['types'] = [_types, _frame.schema.simpleString()]",
        "    if sorted(_actual) != sorted(_rows):",
        "        _entry['status'] = 'fail'",
        "        _entry['missing'] = [r for r in _rows if r not in _actual][:10]",
        "        _entry['unexpected'] = [r for r in _actual if r not in _rows][:10]",
        "        _entry['counts'] = [len(_rows), len(_actual)]",
        "    _report.append(_entry)",
        "import pyspark",
        "dbutils.notebook.exit(json.dumps({'spark_version': spark.version, "
        "'pyspark_client': pyspark.__version__, 'results': _report}))",
    ]
    return "# Databricks notebook source\n" + "\n".join(lines) + "\n"


def validate_pipeline(arguments: argparse.Namespace) -> int:
    """Run every example as one pipeline, twice, and compare its tables with
    local Spark after each run (see the module's description)."""
    from databricks.sdk import WorkspaceClient

    import sparkshift
    from sparkshift.databricks import connect, redact
    from sparkshift.pipelines import pipeline_settings

    catalog, schema = arguments.schema.split(".")
    print("computing the expected results on local Spark", file=sys.stderr)
    _, written = local_results(PIPELINE_PROGRAM)
    expected = {
        table: rows for tables in written.values() for table, rows in tables.items()
    }

    workspace = connect(arguments.profile)
    folder = arguments.folder.rstrip("/")
    workspace.mkdirs(folder)
    load = f"{folder}/load tables"
    workspace.import_notebook(
        load, tables_source(catalog, schema, arguments.pipeline), overwrite=True
    )
    run_driver(arguments.profile, load, arguments.environment)

    sources = [
        (name, path.read_text(), path.name, dialect)
        for name, path, dialect in examples()
    ]
    sources.append(
        (PIPELINE_PROGRAM[0], PIPELINE_PROGRAM[1], "pipeline_steps.sas", "sas")
    )
    paths = []
    for name, text, source, dialect in sources:
        code = sparkshift.to_pipeline(
            sparkshift.convert(text, dialect=dialect),
            source=source,
            dialect=dialect,
            catalog=catalog,
            schema=schema,
        )
        path = f"{folder}/pipeline/{name}.py"
        workspace.mkdirs(path.rsplit("/", 1)[0])
        workspace.import_file(path, code, overwrite=True)
        paths.append(path)
    pipeline_catalog, pipeline_schema = arguments.pipeline.split(".")
    settings = pipeline_settings(
        "sparkshift validation",
        f"{folder}/pipeline",
        paths,
        catalog=pipeline_catalog,
        schema=pipeline_schema,
    )
    existing = workspace.pipelines_named(settings["name"])
    if existing:
        pipeline_id = existing[0]
        workspace.edit_pipeline(pipeline_id, settings)
    else:
        pipeline_id = workspace.create_pipeline(settings)
    print(f"pipeline {pipeline_id}: {len(paths)} source files", file=sys.stderr)

    client = WorkspaceClient(profile=arguments.profile)
    secrets = [client.config.token] if client.config.token else []
    check = f"{folder}/check pipeline"
    workspace.import_notebook(
        check, check_source(expected, arguments.pipeline), overwrite=True
    )
    failed = 0
    for attempt, full_refresh in ((1, True), (2, False)):
        update = client.pipelines.start_update(
            pipeline_id=pipeline_id, full_refresh=full_refresh
        ).update_id
        while True:
            state = client.pipelines.get_update(pipeline_id, update).update.state.value
            if state in ("COMPLETED", "FAILED", "CANCELED"):
                break
            time.sleep(15)
        print(
            f"run {attempt} ({'full refresh' if full_refresh else 'update'}): {state}"
        )
        if state != "COMPLETED":
            for event in client.pipelines.list_pipeline_events(
                pipeline_id=pipeline_id, max_results=50
            ):
                if event.level and event.level.value == "ERROR":
                    for exception in (
                        (event.error.exceptions or []) if event.error else []
                    ):
                        print("  ", redact(str(exception.message), secrets)[:600])
            return 1
        report = run_driver(arguments.profile, check, arguments.environment)
        for entry in report["results"]:
            print(f"{entry['status'].upper():5} {entry['example']}")
            if entry["status"] != "pass":
                failed += 1
                details = {
                    k: v for k, v in entry.items() if k not in ("status", "example")
                }
                print(json.dumps(details, indent=2, ensure_ascii=False))
        matched = len(report["results"]) - sum(
            entry["status"] != "pass" for entry in report["results"]
        )
        print(f"run {attempt}: {matched} of {len(expected)} tables match local Spark")
    return 0 if failed == 0 else 1


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
    parser.add_argument(
        "--write-schema",
        help="also write the notebooks' results to this CATALOG.SCHEMA, "
        "replacing its tables of the same names, and check them",
    )
    parser.add_argument(
        "--pipeline",
        metavar="CATALOG.SCHEMA",
        help="validate the examples as one Lakeflow pipeline, run twice, keeping "
        "its tables in this CATALOG.SCHEMA, instead of as notebooks",
    )
    arguments = parser.parse_args(argv)
    if arguments.pipeline:
        return validate_pipeline(arguments)
    catalog, schema = arguments.schema.split(".")
    write_schema = arguments.write_schema

    print("computing the expected results on local Spark", file=sys.stderr)
    expected, written = local_results()
    upload_examples(arguments.folder, arguments.profile, catalog, schema, write_schema)

    from sparkshift.databricks import connect

    workspace = connect(arguments.profile)
    workspace.mkdirs(arguments.folder)
    driver = f"{arguments.folder}/driver"
    source = driver_source(
        expected, catalog, schema, written if write_schema else None, write_schema
    )
    workspace.import_notebook(driver, source, overwrite=True)
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
    total = len(expected)
    if write_schema:
        total += sum(len(tables) for tables in written.values())
    print(f"{checked - failed} of {total} results and tables match local Spark")
    return 0 if failed == 0 and checked == total else 1


if __name__ == "__main__":
    sys.exit(main())
