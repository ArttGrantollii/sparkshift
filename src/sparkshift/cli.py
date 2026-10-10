"""The command line: ``sparkshift convert query.sql`` and
``sparkshift report queries/``.

``convert`` converts one SQL file, standard input, or every ``.sql`` file in a
directory. Generated code goes to standard output, or to the file or directory
named by ``--output``; every message goes to standard error, so redirecting
the output never captures an error message.

``report`` assesses a SQL file or every ``.sql`` file in a directory and
reports how many convert and what blocks the rest, without writing code.

Exit codes:
    0  every query was converted (convert), or the report was produced
    1  some SQL cannot be converted (convert; standard error lists every
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
from sparkshift.dialects import SUPPORTED_DIALECTS
from sparkshift.errors import SparkShiftError
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
            "Convert SQL queries into PySpark code: one file, standard input, "
            "or every .sql file in a directory. The code assigns the result to "
            "a DataFrame named `result` and expects a SparkSession named `spark`."
        ),
    )
    convert_command.add_argument(
        "input",
        help=f"SQL file or directory to convert, or {_STDIN} to read standard input",
    )
    convert_command.add_argument(
        "-d",
        "--dialect",
        type=str.lower,
        choices=SUPPORTED_DIALECTS,
        help="SQL dialect of the input; omit it for generic SQL",
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

    report_command = commands.add_parser(
        "report",
        help="report how much of a set of SQL files converts, and what blocks the rest",
        description=(
            "Assess a SQL file or every .sql file in a directory: how many "
            "convert, and which constructs block the others, ranked by the "
            "number of files they block. No code is written."
        ),
    )
    report_command.add_argument("input", help="SQL file or directory to assess")
    report_command.add_argument(
        "-d",
        "--dialect",
        type=str.lower,
        choices=SUPPORTED_DIALECTS,
        help="SQL dialect of the input; omit it for generic SQL",
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
    result = _conversion(parser.prog, sql, name, arguments.dialect)
    if result is None:
        return EXIT_NOT_CONVERTED
    if arguments.output is None:
        sys.stdout.write(result.code)
    else:
        _write(parser, arguments.output, result.code)
    return EXIT_CONVERTED


def _convert_directory(
    parser: argparse.ArgumentParser, arguments: argparse.Namespace
) -> int:
    """Convert every .sql file under a directory, in sorted order, into a .py
    file at the same relative path under the output directory. Files that
    cannot be converted are reported, and get no output file."""
    source = Path(arguments.input)
    output: Path | None = arguments.output
    if output is None:
        parser.error(f"converting the directory {source} needs --output DIRECTORY")
    if output.exists() and not output.is_dir():
        parser.error(f"--output {output} must be a directory, not a file")
    files = _sql_files(parser, source)

    converted = 0
    for path in files:
        sql, reason = _read_text(path)
        if sql is None:
            print(f"{parser.prog}: cannot read {path}: {reason}", file=sys.stderr)
            continue
        result = _conversion(parser.prog, sql, str(path), arguments.dialect)
        if result is not None:
            _write(
                parser,
                output / path.relative_to(source).with_suffix(".py"),
                result.code,
            )
            converted += 1

    failed = len(files) - converted
    noun = "file" if len(files) == 1 else "files"
    summary = f"{parser.prog}: converted {converted} of {len(files)} {noun}"
    if failed:
        summary += f"; {failed} could not be converted"
    print(summary, file=sys.stderr)
    return EXIT_CONVERTED if failed == 0 else EXIT_NOT_CONVERTED


def _report(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> int:
    """Assess a file, or every .sql file under a directory, and print or
    write the report."""
    threshold: float | None = arguments.fail_under
    if threshold is not None and not 0 <= threshold <= 100:
        parser.error("--fail-under must be a percentage from 0 to 100")
    source = Path(arguments.input)
    if source.is_dir():
        # Paths relative to the directory, the same on every platform.
        files = [
            (path, path.relative_to(source).as_posix())
            for path in _sql_files(parser, source)
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
            outcomes.append(assess(name, sql, arguments.dialect))
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


def _sql_files(parser: argparse.ArgumentParser, source: Path) -> list[Path]:
    """Every .sql file under a directory, in sorted order. Exits with a usage
    error if there are none."""
    files = sorted(path for path in source.rglob("*.sql") if path.is_file())
    if not files:
        parser.error(f"no .sql files in {source}")
    return files


def _read_text(path: Path) -> tuple[str | None, str | None]:
    """A file's text, or None and the reason it cannot be read."""
    try:
        return path.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return None, "it is not UTF-8 text"
    except OSError as error:
        return None, error.strerror or str(error)


def _conversion(
    prog: str, sql: str, name: str, dialect: str | None
) -> ConversionResult | None:
    """Convert one query, reporting errors and warnings on standard error;
    None if it cannot be converted."""
    try:
        result = convert(sql, dialect=dialect)
    except SparkShiftError as error:
        print(f"{prog}: cannot convert {name}: {error}", file=sys.stderr)
        return None
    for warning in result.warnings:
        print(f"{prog}: warning in {name}: {warning}", file=sys.stderr)
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
