"""The command line: ``sparkshift convert query.sql``.

Converts one SQL file, standard input, or every ``.sql`` file in a directory.
Generated code goes to standard output, or to the file or directory named by
``--output``; every message goes to standard error, so redirecting the output
never captures an error message.

Exit codes:
    0  every query was converted
    1  some SQL cannot be converted; standard error lists every issue
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

EXIT_CONVERTED = 0
EXIT_NOT_CONVERTED = 1
# argparse also exits with 2 for errors in the command line itself.
EXIT_USAGE = 2

_STDIN = "-"


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line with ``argv`` (default: ``sys.argv[1:]``) and
    return the exit code."""
    parser = _parser()
    arguments = parser.parse_args(argv)
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
    files = sorted(path for path in source.rglob("*.sql") if path.is_file())
    if not files:
        parser.error(f"no .sql files in {source}")

    converted = 0
    for path in files:
        try:
            sql = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            reason = (
                "it is not UTF-8 text"
                if isinstance(error, UnicodeDecodeError)
                else error.strerror
            )
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
