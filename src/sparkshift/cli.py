"""The command line: ``sparkshift convert query.sql`` and
``sparkshift report queries/``.

``convert`` converts one SQL file or SAS program, standard input, or every
``.sql`` and ``.sas`` file in a directory. Generated code goes to standard
output, or to the file or directory named by ``--output``; every message goes
to standard error, so redirecting the output never captures an error message.

``report`` assesses a file or every ``.sql`` and ``.sas`` file in a directory
and reports how many convert and what blocks the rest, without writing code.

A ``.sas`` file is always a SAS program; ``--dialect`` gives the dialect of
the SQL files, and ``--dialect sas`` selects only the SAS programs.

Exit codes:
    0  every query was converted (convert), or the report was produced
    1  some input cannot be converted or --strict rejects its warnings
       (convert; standard error lists every
       issue), or fewer files converted than --fail-under requires (report)
    2  usage error: a bad option, or an input or output that cannot be read
       or written
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sparkshift import __version__
from sparkshift.api import convert
from sparkshift.diagnostics import ConversionResult
from sparkshift.dialects import DIALECT_CHOICES, SAS, is_sas
from sparkshift.errors import SparkShiftError
from sparkshift.notebook import to_notebook
from sparkshift.report import Report, assess, to_json, to_markdown, to_text, unreadable

EXIT_CONVERTED = 0
EXIT_NOT_CONVERTED = 1
# argparse also exits with 2 for errors in the command line itself.
EXIT_USAGE = 2

_STDIN = "-"

_REPORT_FORMATS = {"text": to_text, "markdown": to_markdown, "json": to_json}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line with ``argv`` (default: ``sys.argv[1:]``) and
    return the exit code."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "report":
        return _report(parser, arguments)
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
    convert_command.add_argument(
        "--notebook",
        action="store_true",
        help="write Databricks notebooks (source format) instead of plain code",
    )

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
    return parser


def _convert_one(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> int:
    name = "standard input" if arguments.input == _STDIN else arguments.input
    sql = _read(parser, arguments.input)
    dialect = _dialect_of(Path(arguments.input), arguments.dialect)
    result = _conversion(parser.prog, sql, name, dialect, strict=arguments.strict)
    if result is None:
        return EXIT_NOT_CONVERTED
    source = None if arguments.input == _STDIN else arguments.input
    code = _output(result, arguments.notebook, source, dialect)
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
    Files that cannot be converted are reported, and get no output file."""
    source = Path(arguments.input)
    output: Path | None = arguments.output
    if output is None:
        parser.error(f"converting the directory {source} needs --output DIRECTORY")
    if output.exists() and not output.is_dir():
        parser.error(f"--output {output} must be a directory, not a file")
    files = _source_files(parser, source, arguments.dialect)
    destinations = _batch_outputs(parser, source, output, files)

    converted = 0
    for path, destination in destinations:
        sql, reason = _read_text(path)
        if sql is None:
            print(f"{parser.prog}: cannot read {path}: {reason}", file=sys.stderr)
            continue
        dialect = _dialect_of(path, arguments.dialect)
        result = _conversion(
            parser.prog, sql, str(path), dialect, strict=arguments.strict
        )
        if result is not None:
            _write(
                parser,
                destination,
                _output(result, arguments.notebook, path.name, dialect),
            )
            converted += 1

    failed = len(files) - converted
    noun = "file" if len(files) == 1 else "files"
    summary = f"{parser.prog}: converted {converted} of {len(files)} {noun}"
    if failed:
        summary += f"; {failed} could not be converted"
    print(summary, file=sys.stderr)
    return EXIT_CONVERTED if failed == 0 else EXIT_NOT_CONVERTED


def _batch_outputs(
    parser: argparse.ArgumentParser, source: Path, output: Path, files: list[Path]
) -> list[tuple[Path, Path]]:
    """Plan every output before side effects; reject colliding portable names."""
    planned = [
        (path, output / path.relative_to(source).with_suffix(".py")) for path in files
    ]
    by_name: dict[tuple[str, ...], list[Path]] = {}
    for path, destination in planned:
        # Compare whole components, so a.py_extra is not a child of a.py.
        # Case-fold even on Linux: batches should remain portable to Windows.
        name = tuple(part.casefold() for part in destination.relative_to(output).parts)
        by_name.setdefault(name, []).append(path)

    conflicts: set[Path] = set()
    for name, inputs in by_name.items():
        if len(inputs) > 1:
            conflicts.update(inputs)
        for depth in range(1, len(name)):
            if ancestor_inputs := by_name.get(name[:depth]):
                conflicts.update(ancestor_inputs)
                conflicts.update(inputs)

    if conflicts:
        mappings = "\n".join(
            f"  {path} -> {destination}"
            for path, destination in planned
            if path in conflicts
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
            outcomes.append(assess(name, sql, _dialect_of(path, arguments.dialect)))
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


def _source_files(
    parser: argparse.ArgumentParser, source: Path, dialect: str | None
) -> list[Path]:
    """Every .sql and .sas file under a directory (only .sas files with
    --dialect sas), in sorted order. Exits with a usage error if there are
    none."""
    suffixes = (".sas",) if is_sas(dialect) else (".sql", ".sas")
    files = sorted(
        path
        for path in source.rglob("*")
        if path.suffix.lower() in suffixes and path.is_file()
    )
    if not files:
        parser.error(f"no {' or '.join(suffixes)} files in {source}")
    return files


def _output(
    result: ConversionResult, notebook: bool, source: str | None, dialect: str | None
) -> str:
    """The code to write: plain, or as a Databricks notebook."""
    if not notebook:
        return result.code
    return to_notebook(result, source=source, dialect=dialect)


def _dialect_of(path: Path, dialect: str | None) -> str | None:
    """The dialect to convert a file in: a .sas file is a SAS program."""
    return SAS if path.suffix.lower() == ".sas" else dialect


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
