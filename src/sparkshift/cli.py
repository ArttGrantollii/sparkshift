"""The command line: ``sparkshift convert query.sql``,
``sparkshift report queries/``, and ``sparkshift upload queries/ --to FOLDER``.

``convert`` converts one SQL file or SAS program, standard input, or every
``.sql`` and ``.sas`` file in a directory, into plain code, Databricks
notebooks (``--notebook``), or pipeline source files (``--pipeline``).
Generated code goes to standard output, or to the file or directory named by
``--output``; every message goes to standard error, so redirecting the output
never captures an error message.

``report`` assesses a file or every ``.sql`` and ``.sas`` file in a directory
and reports how many convert and what blocks the rest, without writing code.

``upload`` converts a file or every ``.sql`` and ``.sas`` file in a directory
into Databricks notebooks and imports them into a workspace folder (see
:mod:`sparkshift.databricks`), and can create a job that runs them; or, with
``--pipeline``, into pipeline source files and a Lakeflow pipeline.

A ``.sas`` file is always a SAS program; ``--dialect`` gives the dialect of
the SQL files, and ``--dialect sas`` selects only the SAS programs.

Exit codes:
    0  every query was converted (convert) or uploaded (upload), or the
       report was produced
    1  some input cannot be converted or --strict rejects its warnings
       (convert, upload; standard error lists every issue), some notebook
       cannot be imported (upload), or fewer files converted than
       --fail-under requires (report)
    2  usage error: a bad option, an input or output that cannot be read or
       written, or no connection to Databricks (upload)
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path, PurePath, PurePosixPath
from typing import TypeVar

from sparkshift import __version__, databricks
from sparkshift.api import convert
from sparkshift.batch import colliding, dialect_of, shared_datasets, source_suffixes
from sparkshift.diagnostics import ConversionResult
from sparkshift.dialects import DIALECT_CHOICES
from sparkshift.errors import SparkShiftError
from sparkshift.jobs import default_job_name, job_settings
from sparkshift.notebook import to_notebook
from sparkshift.pipelines import (
    default_pipeline_name,
    pipeline_settings,
    pipeline_tables,
    to_pipeline,
)
from sparkshift.report import Report, assess, to_json, to_markdown, to_text, unreadable

EXIT_CONVERTED = 0
EXIT_NOT_CONVERTED = 1
# argparse also exits with 2 for errors in the command line itself.
EXIT_USAGE = 2

_STDIN = "-"

_REPORT_FORMATS = {"text": to_text, "markdown": to_markdown, "json": to_json}

PathT = TypeVar("PathT", bound=PurePath)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line with ``argv`` (default: ``sys.argv[1:]``) and
    return the exit code."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "report":
        return _report(parser, arguments)
    if arguments.command == "upload":
        return _upload(parser, arguments)
    reading = (arguments.catalog, arguments.schema)
    writing = (arguments.output_catalog, arguments.output_schema)
    if arguments.pipeline:
        if any(writing):
            parser.error(
                "--output-catalog and --output-schema are for notebooks; a "
                "pipeline keeps its tables in the catalog and schema it is "
                "created with"
            )
        if arguments.catalog and not arguments.schema:
            parser.error("--catalog needs --schema with --pipeline")
    elif any(reading + writing) and not arguments.notebook:
        parser.error(
            "--catalog, --schema, --output-catalog and --output-schema need "
            "--notebook (--catalog and --schema also work with --pipeline)"
        )
    if arguments.input != _STDIN and Path(arguments.input).is_dir():
        return _convert_directory(parser, arguments)
    return _convert_one(parser, arguments)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sparkshift",
        description="Convert SQL into readable, tested PySpark DataFrame code.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    convert_command = commands.add_parser(
        "convert",
        help="convert SQL queries into PySpark code",
        description=(
            "Convert SQL queries and SAS programs into PySpark code: one file, "
            "standard input, or every .sql and .sas file in a directory. The "
            "code assigns the result to a DataFrame named `result` and expects "
            "a SparkSession named `spark`."
        ),
    )
    convert_command.add_argument(
        "input",
        help=f"file or directory to convert, or {_STDIN} to read standard input",
    )
    convert_command.add_argument(
        "-d",
        "--dialect",
        type=str.lower,
        choices=DIALECT_CHOICES,
        help="SQL dialect of the input, or sas for SAS programs; omit it for "
        "generic SQL. A .sas file is always a SAS program",
    )
    convert_command.add_argument(
        "-o",
        "--output",
        type=Path,
        help=(
            "write the code to this file instead of standard output; for a "
            "directory input, the directory to write .py files into (required)"
        ),
    )
    kinds = convert_command.add_mutually_exclusive_group()
    kinds.add_argument(
        "--notebook",
        action="store_true",
        help="write Databricks notebooks (source format) instead of plain code",
    )
    kinds.add_argument(
        "--pipeline",
        action="store_true",
        help="write pipeline source files, for Lakeflow pipelines on Databricks "
        "or spark-pipelines on Apache Spark: each data set, or a query's result, "
        "becomes a materialized view",
    )
    _add_location_arguments(convert_command, "with --notebook, ")

    convert_command.add_argument(
        "--strict",
        action="store_true",
        help="reject conversions with any warnings; do not write their output",
    )

    report_command = commands.add_parser(
        "report",
        help="report how much of a set of files converts, and what blocks the rest",
        description=(
            "Assess a file or every .sql and .sas file in a directory: how many "
            "convert, and which constructs block the others, ranked by the "
            "number of files they block. No code is written."
        ),
    )
    report_command.add_argument("input", help="file or directory to assess")
    report_command.add_argument(
        "-d",
        "--dialect",
        type=str.lower,
        choices=DIALECT_CHOICES,
        help="SQL dialect of the input, or sas for SAS programs only; omit it "
        "for generic SQL. A .sas file is always a SAS program",
    )
    report_command.add_argument(
        "-f",
        "--format",
        choices=tuple(_REPORT_FORMATS),
        default="text",
        help="text for a terminal (default), markdown, or json",
    )
    report_command.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write the report to this file instead of standard output",
    )
    report_command.add_argument(
        "--fail-under",
        type=float,
        metavar="PERCENT",
        help="exit with status 1 if fewer than PERCENT of the files convert",
    )

    upload_command = commands.add_parser(
        "upload",
        help="convert files into notebooks and import them into Databricks",
        description=(
            "Convert a .sql or .sas file, or every one in a directory, into "
            "Databricks notebooks and import them into a workspace folder, "
            "keeping their relative paths. Credentials come from the "
            "DATABRICKS_HOST and DATABRICKS_TOKEN environment variables or a "
            "~/.databrickscfg profile, never from the command line. Needs "
            f"{databricks.INSTALL_HINT}, except with --dry-run."
        ),
    )
    upload_command.add_argument("input", help="file or directory to upload")
    upload_command.add_argument(
        "--to",
        required=True,
        metavar="FOLDER",
        help="workspace folder to import into, such as "
        "/Workspace/Users/you@example.com/migrated",
    )
    upload_command.add_argument(
        "-d",
        "--dialect",
        type=str.lower,
        choices=DIALECT_CHOICES,
        help="SQL dialect of the input, or sas for SAS programs only; omit it "
        "for generic SQL. A .sas file is always a SAS program",
    )
    _add_location_arguments(upload_command, "")
    upload_command.add_argument(
        "--profile",
        help="the ~/.databrickscfg profile to authenticate with",
    )
    upload_command.add_argument(
        "--overwrite",
        action="store_true",
        help="replace notebooks that already exist (by default they are kept "
        "and reported)",
    )
    upload_command.add_argument(
        "--strict",
        action="store_true",
        help="reject conversions with any warnings; do not upload them",
    )
    upload_command.add_argument(
        "--dry-run",
        action="store_true",
        help="convert and list the notebook paths without connecting to Databricks",
    )
    deployments = upload_command.add_mutually_exclusive_group()
    deployments.add_argument(
        "--job",
        nargs="?",
        const="",
        metavar="NAME",
        help="also create a job that runs the notebooks in order on serverless "
        "compute, named NAME (default: sparkshift and the folder's name); with "
        "--overwrite, a job of that name is updated. The job is created only if "
        "every file converts and uploads",
    )
    deployments.add_argument(
        "--pipeline",
        nargs="?",
        const="",
        metavar="NAME",
        help="upload pipeline source files instead of notebooks, and create a "
        "Lakeflow pipeline from them on serverless compute, named NAME (default: "
        "sparkshift and the folder's name), keeping its tables in "
        "--output-catalog and --output-schema; with --overwrite, a pipeline of "
        "that name is updated. The pipeline is created only if every file "
        "converts and uploads",
    )
    return parser


def _add_location_arguments(command: argparse.ArgumentParser, condition: str) -> None:
    """--catalog, --schema, --output-catalog and --output-schema: the
    notebooks' widget values."""
    for name in ("catalog", "schema"):
        command.add_argument(
            f"--{name}",
            default="",
            help=f"{condition}the {name} the notebooks read tables from, as the "
            f"initial value of their {name} widget (default: the current one)",
        )
    command.add_argument(
        "--output-catalog",
        default="",
        help=f"{condition}the catalog the notebooks write their results to, as "
        "the initial value of their output_catalog widget (default: the current "
        "one)",
    )
    command.add_argument(
        "--output-schema",
        default="",
        help=f"{condition}the schema the notebooks write their results to, "
        "replacing tables of the same names, as the initial value of their "
        "output_schema widget (default: none, nothing is written)",
    )


def _convert_one(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> int:
    name = "standard input" if arguments.input == _STDIN else arguments.input
    sql = _read(parser, arguments.input)
    dialect = dialect_of(Path(arguments.input), arguments.dialect)
    result = _conversion(parser.prog, sql, name, dialect, strict=arguments.strict)
    if result is None:
        return EXIT_NOT_CONVERTED
    source = None if arguments.input == _STDIN else arguments.input
    code = _output(parser.prog, result, arguments, source, dialect, name)
    if code is None:
        return EXIT_NOT_CONVERTED
    if arguments.output is None:
        sys.stdout.write(code)
    else:
        _write(parser, arguments.output, code)
    return EXIT_CONVERTED


def _convert_directory(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace
) -> int:
    """Convert every .sql and .sas file under a directory, in sorted order,
    into a .py file at the same relative path under the output directory.
    Files that cannot be converted are reported, and get no output file. The
    pipeline source files of a directory are one pipeline: files that define
    a dataset of the same name are reported too."""
    source = Path(arguments.input)
    output: Path | None = arguments.output
    if output is None:
        parser.error(f"converting the directory {source} needs --output DIRECTORY")
    if output.exists() and not output.is_dir():
        parser.error(f"--output {output} must be a directory, not a file")
    files = _source_files(parser, source, arguments.dialect)
    destinations = _batch_outputs(parser, source, output, files)

    outputs: list[tuple[Path, Path, str, list[str]]] = []
    for path, destination in destinations:
        sql, reason = _read_text(path)
        if sql is None:
            print(f"{parser.prog}: cannot read {path}: {reason}", file=sys.stderr)
            continue
        dialect = dialect_of(path, arguments.dialect)
        result = _conversion(
            parser.prog, sql, str(path), dialect, strict=arguments.strict
        )
        if result is None:
            continue
        code = _output(parser.prog, result, arguments, path.name, dialect, str(path))
        if code is None:
            continue
        tables = (
            pipeline_tables(result, source=path.name, dialect=dialect)
            if arguments.pipeline
            else []
        )
        outputs.append((path, destination, code, tables))
    if arguments.pipeline:
        outputs = _defined_once(parser.prog, outputs)
    for _, destination, code, _ in outputs:
        _write(parser, destination, code)

    converted = len(outputs)
    failed = len(files) - converted
    noun = "file" if len(files) == 1 else "files"
    summary = f"{parser.prog}: converted {converted} of {len(files)} {noun}"
    if failed:
        summary += f"; {failed} could not be converted"
    print(summary, file=sys.stderr)
    return EXIT_CONVERTED if failed == 0 else EXIT_NOT_CONVERTED


def _defined_once(
    prog: str, outputs: list[tuple[Path, PathT, str, list[str]]]
) -> list[tuple[Path, PathT, str, list[str]]]:
    """The outputs whose datasets no other file of the pipeline defines; the
    others are reported, as a pipeline defines each dataset once."""
    reasons = shared_datasets([(str(path), tables) for path, _, _, tables in outputs])
    for position, reason in sorted(reasons.items()):
        print(
            f"{prog}: cannot convert {outputs[position][0]}: {reason}", file=sys.stderr
        )
    return [
        output for position, output in enumerate(outputs) if position not in reasons
    ]


def _batch_outputs(
    parser: argparse.ArgumentParser,
    source: Path,
    output: PathT,
    files: list[Path],
    suffix: str = ".py",
) -> list[tuple[Path, PathT]]:
    """Plan every output before side effects; reject colliding portable names.

    ``output`` is a local directory, or a workspace folder as a
    ``PurePosixPath``; each input keeps its relative path, with ``suffix``.
    """
    planned = [
        (path, output.joinpath(*path.relative_to(source).with_suffix(suffix).parts))
        for path in files
    ]
    conflicts = colliding(
        [destination.relative_to(output) for _, destination in planned]
    )
    if conflicts:
        mappings = "\n".join(
            f"  {path} -> {destination}"
            for index, (path, destination) in enumerate(planned)
            if index in conflicts
        )
        parser.error(
            f"batch output paths collide:\n{mappings}\n"
            "No files were written. Rename the inputs or place them in separate "
            "subdirectories so their output paths are distinct (ignoring case)."
        )
    return planned


def _report(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> int:
    """Assess a file, or every .sql and .sas file under a directory, and
    print or write the report."""
    threshold: float | None = arguments.fail_under
    if threshold is not None and not 0 <= threshold <= 100:
        parser.error("--fail-under must be a percentage from 0 to 100")
    source = Path(arguments.input)
    if source.is_dir():
        # Paths relative to the directory, the same on every platform.
        files = [
            (path, path.relative_to(source).as_posix())
            for path in _source_files(parser, source, arguments.dialect)
        ]
    elif source.is_file():
        files = [(source, arguments.input)]
    else:
        parser.error(f"cannot read {arguments.input}: no such file or directory")

    outcomes = []
    for path, name in files:
        sql, reason = _read_text(path)
        if sql is None:
            outcomes.append(unreadable(name, str(reason)))
        else:
            outcomes.append(assess(name, sql, dialect_of(path, arguments.dialect)))
    report = Report(arguments.input, arguments.dialect, tuple(outcomes))

    text = _REPORT_FORMATS[arguments.format](report)
    if arguments.output is None:
        sys.stdout.write(text)
    else:
        _write(parser, arguments.output, text)

    # Compare exact counts: a rounded 90.0% may really be 89.96%.
    if threshold is not None and report.converted * 100 < threshold * report.total:
        print(
            f"{parser.prog}: converted {report.converted} of {report.total} files "
            f"({report.converted_percent}%), below --fail-under {threshold:g}%",
            file=sys.stderr,
        )
        return EXIT_NOT_CONVERTED
    return EXIT_CONVERTED


def _upload(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> int:
    """Convert a file, or every .sql and .sas file under a directory, into
    notebooks, or pipeline source files, and import them into a workspace
    folder. Every file is converted before connecting; a file that cannot be
    imported is reported, and the others are still imported."""
    pipeline = arguments.pipeline is not None
    if pipeline and not (arguments.output_catalog and arguments.output_schema):
        parser.error(
            "--pipeline needs --output-catalog and --output-schema: the catalog "
            "and schema the pipeline keeps its tables in"
        )
    if pipeline and arguments.catalog and not arguments.schema:
        parser.error("--catalog needs --schema with --pipeline")
    folder = PurePosixPath(arguments.to)
    if not folder.is_absolute() or ".." in folder.parts:
        parser.error(
            f"--to {arguments.to} must be an absolute workspace folder, such as "
            "/Workspace/Users/you@example.com/migrated"
        )
    source = Path(arguments.input)
    if source.is_dir():
        files = _source_files(parser, source, arguments.dialect)
    elif source.is_file():
        files, source = [source], source.parent
    else:
        parser.error(f"cannot read {arguments.input}: no such file or directory")
    # Notebooks have no suffix in a workspace; pipeline source files are files.
    suffix = ".py" if pipeline else ""
    planned = _batch_outputs(parser, source, folder, files, suffix=suffix)

    outputs: list[tuple[Path, PurePosixPath, str, list[str]]] = []
    for path, target in planned:
        sql, reason = _read_text(path)
        if sql is None:
            print(f"{parser.prog}: cannot read {path}: {reason}", file=sys.stderr)
            continue
        dialect = dialect_of(path, arguments.dialect)
        result = _conversion(
            parser.prog, sql, str(path), dialect, strict=arguments.strict
        )
        if result is None:
            continue
        if not pipeline:
            notebook = to_notebook(
                result,
                source=path.name,
                dialect=dialect,
                catalog=arguments.catalog,
                schema=arguments.schema,
                output_catalog=arguments.output_catalog,
                output_schema=arguments.output_schema,
            )
            outputs.append((path, target, notebook, []))
            continue
        try:
            code = to_pipeline(
                result,
                source=path.name,
                dialect=dialect,
                catalog=arguments.catalog,
                schema=arguments.schema,
            )
        except SparkShiftError as error:
            print(f"{parser.prog}: cannot convert {path}: {error}", file=sys.stderr)
            continue
        tables = pipeline_tables(result, source=path.name, dialect=dialect)
        outputs.append((path, target, code, tables))
    if pipeline:
        outputs = _defined_once(parser.prog, outputs)

    settings = None
    paths = [str(target) for _, target, _, _ in outputs]
    if arguments.job is not None:
        settings = job_settings(
            arguments.job or default_job_name(str(folder)),
            str(folder),
            paths,
            catalog=arguments.catalog,
            schema=arguments.schema,
            output_catalog=arguments.output_catalog,
            output_schema=arguments.output_schema,
        )
    elif pipeline:
        settings = pipeline_settings(
            arguments.pipeline or default_pipeline_name(str(folder)),
            str(folder),
            paths,
            catalog=arguments.output_catalog,
            schema=arguments.output_schema,
        )

    if arguments.dry_run:
        for path, target, _, _ in outputs:
            print(f"{path} -> {target}")
        status = _upload_summary(
            parser.prog, "would upload", "converted", len(outputs), files
        )
        if settings is not None and status == EXIT_CONVERTED:
            if pipeline:
                print(
                    f"pipeline {settings['name']!r}: {len(paths)} source "
                    f"{'file' if len(paths) == 1 else 'files'}, tables in "
                    f"{settings['catalog']}.{settings['schema']}"
                )
            else:
                keys = " -> ".join(task["task_key"] for task in settings["tasks"])
                print(f"job {settings['name']!r}: {keys}")
        return status

    try:
        workspace = databricks.connect(arguments.profile)
    except databricks.UploadError as error:
        print(f"{parser.prog}: {error}", file=sys.stderr)
        return EXIT_USAGE
    uploaded = 0
    created: set[PurePosixPath] = set()
    upload_one = workspace.import_file if pipeline else workspace.import_notebook
    for path, target, code, _ in outputs:
        try:
            if target.parent not in created:
                workspace.mkdirs(str(target.parent))
                created.add(target.parent)
            upload_one(str(target), code, overwrite=arguments.overwrite)
        except databricks.UploadError as error:
            print(
                f"{parser.prog}: cannot upload {path} to {target}: {error}",
                file=sys.stderr,
            )
            continue
        print(f"{parser.prog}: uploaded {path} to {target}", file=sys.stderr)
        uploaded += 1
    status = _upload_summary(
        parser.prog, "uploaded", "converted or uploaded", uploaded, files
    )
    if settings is None:
        return status
    kind = "pipeline" if pipeline else "job"
    if status != EXIT_CONVERTED:
        # A job or pipeline without one of its files would quietly compute
        # something else.
        print(
            f"{parser.prog}: no {kind} created: it needs every file converted and "
            "uploaded",
            file=sys.stderr,
        )
        return status
    if pipeline:
        return _create_pipeline(
            parser.prog, workspace, settings, overwrite=arguments.overwrite
        )
    return _create_job(parser.prog, workspace, settings, overwrite=arguments.overwrite)


def _create_job(
    prog: str, workspace: databricks.Workspace, settings: dict, *, overwrite: bool
) -> int:
    """Create the job, or update the one of the same name with --overwrite."""
    name = settings["name"]
    steps = len(settings["tasks"])
    noun = "notebook" if steps == 1 else "notebooks"
    try:
        existing = workspace.jobs_named(name)
        if len(existing) > 1:
            print(
                f"{prog}: no job created: {len(existing)} jobs are named {name!r}; "
                "delete or rename all but one",
                file=sys.stderr,
            )
            return EXIT_NOT_CONVERTED
        if existing and not overwrite:
            print(
                f"{prog}: no job created: a job named {name!r} already exists "
                f"(ID {existing[0]}); pass --overwrite to update it",
                file=sys.stderr,
            )
            return EXIT_NOT_CONVERTED
        if existing:
            workspace.reset_job(existing[0], settings)
            done, job_id = "updated", existing[0]
        else:
            done, job_id = "created", workspace.create_job(settings)
    except databricks.UploadError as error:
        print(f"{prog}: cannot create the job {name!r}: {error}", file=sys.stderr)
        return EXIT_NOT_CONVERTED
    print(
        f"{prog}: {done} the job {name!r} (ID {job_id}): runs {steps} {noun} in order",
        file=sys.stderr,
    )
    return EXIT_CONVERTED


def _create_pipeline(
    prog: str, workspace: databricks.Workspace, settings: dict, *, overwrite: bool
) -> int:
    """Create the pipeline, or update the one of the same name with
    --overwrite."""
    name = settings["name"]
    files = len(settings["libraries"])
    try:
        existing = workspace.pipelines_named(name)
        if len(existing) > 1:
            print(
                f"{prog}: no pipeline created: {len(existing)} pipelines are named "
                f"{name!r}; delete or rename all but one",
                file=sys.stderr,
            )
            return EXIT_NOT_CONVERTED
        if existing and not overwrite:
            print(
                f"{prog}: no pipeline created: a pipeline named {name!r} already "
                f"exists (ID {existing[0]}); pass --overwrite to update it",
                file=sys.stderr,
            )
            return EXIT_NOT_CONVERTED
        if existing:
            workspace.edit_pipeline(existing[0], settings)
            done, pipeline_id = "updated", existing[0]
        else:
            done, pipeline_id = "created", workspace.create_pipeline(settings)
    except databricks.UploadError as error:
        print(f"{prog}: cannot create the pipeline {name!r}: {error}", file=sys.stderr)
        return EXIT_NOT_CONVERTED
    noun = "file" if files == 1 else "files"
    print(
        f"{prog}: {done} the pipeline {name!r} (ID {pipeline_id}): {files} source "
        f"{noun}, tables in {settings['catalog']}.{settings['schema']}; start it "
        "under Jobs & Pipelines",
        file=sys.stderr,
    )
    return EXIT_CONVERTED


def _upload_summary(
    prog: str, done: str, failure: str, count: int, files: list[Path]
) -> int:
    noun = "file" if len(files) == 1 else "files"
    summary = f"{prog}: {done} {count} of {len(files)} {noun}"
    failed = len(files) - count
    if failed:
        summary += f"; {failed} could not be {failure}"
    print(summary, file=sys.stderr)
    return EXIT_CONVERTED if failed == 0 else EXIT_NOT_CONVERTED


def _source_files(
    parser: argparse.ArgumentParser, source: Path, dialect: str | None
) -> list[Path]:
    """Every .sql and .sas file under a directory (only .sas files with
    --dialect sas), in sorted order. Exits with a usage error if there are
    none."""
    suffixes = source_suffixes(dialect)
    files = sorted(
        path
        for path in source.rglob("*")
        if path.suffix.lower() in suffixes and path.is_file()
    )
    if not files:
        parser.error(f"no {' or '.join(suffixes)} files in {source}")
    return files


def _output(
    prog: str,
    result: ConversionResult,
    arguments: argparse.Namespace,
    source: str | None,
    dialect: str | None,
    name: str,
) -> str | None:
    """The code to write: plain, as a Databricks notebook, or as a pipeline
    source file; None, after reporting why, for a program that cannot be a
    pipeline."""
    if arguments.pipeline:
        try:
            return to_pipeline(
                result,
                source=source,
                dialect=dialect,
                catalog=arguments.catalog,
                schema=arguments.schema,
            )
        except SparkShiftError as error:
            print(f"{prog}: cannot convert {name}: {error}", file=sys.stderr)
            return None
    if not arguments.notebook:
        return result.code
    return to_notebook(
        result,
        source=source,
        dialect=dialect,
        catalog=arguments.catalog,
        schema=arguments.schema,
        output_catalog=arguments.output_catalog,
        output_schema=arguments.output_schema,
    )


def _read_text(path: Path) -> tuple[str | None, str | None]:
    """A file's text, or None and the reason it cannot be read."""
    try:
        return path.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, "it is not UTF-8 text"
    except OSError as error:
        return None, error.strerror or str(error)


def _conversion(
    prog: str, sql: str, name: str, dialect: str | None, *, strict: bool
) -> ConversionResult | None:
    """Convert one query, reporting errors and warnings on standard error;
    None if it cannot be converted or strict mode rejects its warnings."""
    try:
        result = convert(sql, dialect=dialect)
    except SparkShiftError as error:
        print(f"{prog}: cannot convert {name}: {error}", file=sys.stderr)
        return None
    for warning in result.warnings:
        print(f"{prog}: warning in {name}: {warning}", file=sys.stderr)
    if strict and result.warnings:
        print(
            f"{prog}: --strict rejected {name} because conversion has warnings; "
            "no output written for this input",
            file=sys.stderr,
        )
        return None
    return result


def _read(parser: argparse.ArgumentParser, source: str) -> str:
    """The SQL to convert, from a file or standard input. Exits with a usage
    error if it cannot be read."""
    if source == _STDIN:
        return sys.stdin.read()
    try:
        return Path(source).read_text(encoding="utf-8")
    except OSError as error:
        parser.error(f"cannot read {source}: {error.strerror}")
    except UnicodeDecodeError:
        parser.error(f"cannot read {source}: it is not UTF-8 text")


def _write(parser: argparse.ArgumentParser, path: Path, code: str) -> None:
    """Write generated code, creating directories as needed. Exits with a
    usage error if it cannot be written."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n" keeps line endings the same on every platform.
        path.write_text(code, encoding="utf-8", newline="\n")
    except OSError as error:
        parser.error(f"cannot write {path}: {error.strerror}")
