"""The command line: ``sparkshift convert query.sql``.

Generated code goes to standard output, or to the file named by ``--output``;
every message goes to standard error, so redirecting the output never
captures an error message.

Exit codes:
    0  the query was converted
    1  the SQL cannot be converted; standard error lists every issue
    2  usage error: a bad option, or an input or output file that cannot be
       read or written
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sparkshift import __version__
from sparkshift.api import convert
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
    return _convert(parser, arguments)


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
        help="convert one SQL query into PySpark code",
        description=(
            "Convert one SQL query into PySpark code. The code assigns the "
            "result to a DataFrame named `result` and expects a SparkSession "
            "named `spark`."
        ),
    )
    convert_command.add_argument(
        "input", help=f"SQL file to convert, or {_STDIN} to read standard input"
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
        help="write the code to this file instead of standard output",
    )
    return parser


def _convert(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> int:
    name = "standard input" if arguments.input == _STDIN else arguments.input
    sql = _read(parser, arguments.input)
    try:
        result = convert(sql, dialect=arguments.dialect)
    except SparkShiftError as error:
        print(f"{parser.prog}: cannot convert {name}: {error}", file=sys.stderr)
        return EXIT_NOT_CONVERTED

    for warning in result.warnings:
        print(f"{parser.prog}: warning: {warning}", file=sys.stderr)
    if arguments.output is None:
        sys.stdout.write(result.code)
        return EXIT_CONVERTED
    try:
        # newline="\n" keeps line endings the same on every platform.
        arguments.output.write_text(result.code, encoding="utf-8", newline="\n")
    except OSError as error:
        parser.error(f"cannot write {arguments.output}: {error.strerror}")
    return EXIT_CONVERTED


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
