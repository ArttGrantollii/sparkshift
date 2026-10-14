"""Generated code as a Databricks notebook, in Databricks' source format.

A source-format notebook is a Python file that starts with
``# Databricks notebook source``; ``# COMMAND ----------`` separates its
cells, and a cell whose lines start with ``# MAGIC %md`` is Markdown. Databricks
imports such a file as a notebook, and it stays a readable, diffable Python
file (see docs/adr/0007-databricks-notebooks.md).

The notebook has a Markdown cell that says where the code comes from and
lists the warnings to review, then the generated code, one cell per section
(the imports, the table and window variables, each DataFrame, and the
result), and finally ``display(result)``. ``spark`` and ``display`` are
predefined in Databricks notebooks.
"""

from pathlib import PurePath

from sparkshift.diagnostics import ConversionResult, Diagnostic
from sparkshift.dialects import is_sas

HEADER = "# Databricks notebook source"
CELL_SEPARATOR = "# COMMAND ----------"
_MAGIC = "# MAGIC"

# How a notebook names the language it was converted from.
_LANGUAGES = {
    None: "generic SQL",
    "tsql": "T-SQL",
    "postgres": "PostgreSQL",
    "mysql": "MySQL",
    "snowflake": "Snowflake",
    "bigquery": "BigQuery",
    "oracle": "Oracle",
}


def to_notebook(
    result: ConversionResult,
    *,
    source: str | None = None,
    dialect: str | None = None,
) -> str:
    """A Databricks source-format notebook that runs ``result.code``.

    ``source`` names the file the code was converted from, for the title,
    and ``dialect`` is the language it was converted from, as given to
    ``convert`` ("sas" for SAS).
    """
    cells = [_markdown(_description(result, source, dialect))]
    cells += _code_cells(result.code)
    cells.append("display(result)")
    body = f"\n\n{CELL_SEPARATOR}\n\n".join(cells)
    return f"{HEADER}\n{body}\n"


def _description(
    result: ConversionResult, source: str | None, dialect: str | None
) -> list[str]:
    from sparkshift import __version__

    title = PurePath(source).stem if source else "Converted code"
    key = dialect.lower() if dialect else None
    language = "SAS" if is_sas(dialect) else _LANGUAGES.get(key, dialect)
    origin = f"`{PurePath(source).name}`" if source else "code"
    lines = [
        f"# {title}",
        "",
        f"Converted from {origin} ({language}) by SparkShift {__version__}. "
        "Tables are read by name from the current catalog and schema: run "
        "`USE CATALOG` and `USE SCHEMA` first if they are elsewhere.",
    ]
    if result.warnings:
        lines += ["", "**Review before running:**", ""]
        lines += [_warning(warning) for warning in result.warnings]
    return lines


def _warning(warning: Diagnostic) -> str:
    text = f"- {warning.message}: {_code_span(warning.sql)}"
    if warning.hint:
        text += f". {warning.hint}"
    return text


def _code_span(text: str) -> str:
    """Text as Markdown code, with enough backticks around it."""
    fence = "``" if "`" in text else "`"
    padding = " " if fence == "``" else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def _markdown(lines: list[str]) -> str:
    # Metadata can contain newlines, including notebook cell markers. Prefix
    # every physical line so it cannot become Python or a new notebook cell.
    physical_lines = "\n".join(lines).splitlines()
    magic = [f"{_MAGIC} %md"] + [f"{_MAGIC} {line}".rstrip() for line in physical_lines]
    return "\n".join(magic)


def _code_cells(code: str) -> list[str]:
    """The code's sections, separated by blank lines, as cells; consecutive
    import sections share a cell."""
    cells: list[str] = []
    for section in code.strip("\n").split("\n\n"):
        is_import = section.startswith(("from ", "import "))
        if is_import and cells and cells[-1].startswith(("from ", "import ")):
            cells[-1] += "\n" + section
        else:
            cells.append(section)
    return cells
