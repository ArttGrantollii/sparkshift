"""Generated code as a Databricks notebook, in Databricks' source format.

A source-format notebook is a Python file that starts with
``# Databricks notebook source``; ``# COMMAND ----------`` separates its
cells, and a cell whose lines start with ``# MAGIC %md`` is Markdown. Databricks
imports such a file as a notebook, and it stays a readable, diffable Python
file (see docs/adr/0007-databricks-notebooks.md).

The notebook has a Markdown cell that says where the code comes from, that
it needs a PySpark 4 client, which tables it can write, and the warnings to
review; a cell with ``catalog`` and ``schema`` widgets that choose where
tables are read from, and ``output_catalog`` and ``output_schema`` widgets
that choose where results are written; then the generated code, one cell per
section (the imports, the table and window variables, each DataFrame, and the
result); a cell that writes the results when ``output_schema`` is set; and
finally ``display(result)``. ``spark``, ``dbutils``, and ``display`` are
predefined in Databricks notebooks.

The results written are each data set a SAS program creates, as a table of
the same name, or a SQL query's result, as a table named after its file.

``notebook_cells`` describes the same cells, for a page that shows them as
Databricks does: Markdown formatted, and the setup cells, which are the same
in every notebook, folded.
"""

import re
from dataclasses import dataclass
from pathlib import PurePath

from sparkshift.diagnostics import ConversionResult, Diagnostic
from sparkshift.dialects import is_sas
from sparkshift.emit import python_string

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


@dataclass(frozen=True)
class Cell:
    """A cell of a notebook: ``kind`` is "markdown", "setup" (the widgets,
    and the writing of results to tables, the same in every notebook), or
    "code"; ``source`` is the cell as the notebook file has it, and ``title``
    says what a setup cell does."""

    kind: str
    source: str
    title: str = ""


def to_notebook(
    result: ConversionResult,
    *,
    source: str | None = None,
    dialect: str | None = None,
    catalog: str = "",
    schema: str = "",
    output_catalog: str = "",
    output_schema: str = "",
) -> str:
    """A Databricks source-format notebook that runs ``result.code``.

    ``source`` names the file the code was converted from, for the title and
    the name of a SQL query's table, and ``dialect`` is the language it was
    converted from, as given to ``convert`` ("sas" for SAS). ``catalog`` and
    ``schema`` are the initial values of the notebook's ``catalog`` and
    ``schema`` widgets, which select where tables are read from; empty means
    the current catalog or schema. ``output_catalog`` and ``output_schema``
    are those of the widgets that select where results are written: nothing
    is written while ``output_schema`` is empty, and an empty
    ``output_catalog`` means the current catalog.
    """
    cells = notebook_cells(
        result,
        source=source,
        dialect=dialect,
        catalog=catalog,
        schema=schema,
        output_catalog=output_catalog,
        output_schema=output_schema,
    )
    body = f"\n\n{CELL_SEPARATOR}\n\n".join(cell.source for cell in cells)
    return f"{HEADER}\n{body}\n"


def notebook_cells(
    result: ConversionResult,
    *,
    source: str | None = None,
    dialect: str | None = None,
    catalog: str = "",
    schema: str = "",
    output_catalog: str = "",
    output_schema: str = "",
) -> list[Cell]:
    """The cells of the notebook ``to_notebook`` writes, with the same
    arguments, in order."""
    tables = _tables(result, source, dialect)
    cells = [
        Cell("markdown", markdown_cell(_description(result, source, dialect, tables)))
    ]
    cells.append(
        Cell(
            "setup",
            _location_cell(catalog, schema, output_catalog, output_schema),
            "Widgets: the catalog and schema to read from and write to",
        )
    )
    cells += [Cell("code", code) for code in _code_cells(result.code)]
    if tables:
        cells.append(
            Cell(
                "setup", _write_cell(result.code, tables), "Write the results to tables"
            )
        )
    cells.append(Cell("code", "display(result)"))
    return cells


def markdown_text(cell: str) -> str:
    """The Markdown of a Markdown cell, without its ``# MAGIC`` prefixes."""
    lines = cell.split("\n")[1:]  # after "# MAGIC %md"
    return "\n".join(line.removeprefix(_MAGIC).removeprefix(" ") for line in lines)


def _tables(
    result: ConversionResult, source: str | None, dialect: str | None
) -> list[tuple[str, str]]:
    """The variable and table name of each result the notebook can write.

    A SAS program's data sets keep their names; a program that creates a
    data set twice writes the second, as SAS replaces the first, and one
    that only prints creates none. A SQL query's result is named after its
    file, or ``result`` without one.
    """
    if not result.datasets and not is_sas(dialect):
        return [("result", table_name(source))]
    seen: set[str] = set()
    last: list[tuple[str, str]] = []
    for dataset in reversed(result.datasets):
        if dataset.name not in seen:
            seen.add(dataset.name)
            last.append((dataset.variable, dataset.name))
    return last[::-1]


def table_name(source: str | None) -> str:
    """A table name from a file name: its stem in lowercase, with characters
    other than ASCII letters, digits and underscores replaced by underscores."""
    stem = PurePath(source).stem if source else ""
    return re.sub(r"[^a-z0-9_]", "_", stem.lower()) or "result"


def language_name(dialect: str | None) -> str | None:
    """How a notebook names the language it was converted from."""
    if is_sas(dialect):
        return "SAS"
    return _LANGUAGES.get(dialect.lower() if dialect else None, dialect)


def _description(
    result: ConversionResult,
    source: str | None,
    dialect: str | None,
    tables: list[tuple[str, str]],
) -> list[str]:
    from sparkshift import __version__

    title = PurePath(source).stem if source else "Converted code"
    language = language_name(dialect)
    origin = f"`{PurePath(source).name}`" if source else "code"
    lines = [
        f"# {title}",
        "",
        f"Converted from {origin} ({language}) by SparkShift {__version__}. "
        "Tables are read by name from the catalog and schema in the `catalog` "
        "and `schema` widgets at the top of the notebook; leave a widget empty "
        "to use the current one.",
        "",
        # Measured: serverless environments 2 and 3 have a PySpark 3.5 client,
        # without Column.outer() (docs/databricks-validation.md).
        "Needs a PySpark 4 client: on serverless compute, use environment "
        "version 4 or later.",
        "",
        f"Set the `output_schema` widget to save {_written(result, tables)} in "
        "that schema, in the catalog in the `output_catalog` widget or the "
        "current one. Tables of the same name are replaced. Leave "
        "`output_schema` empty to only display the result."
        if tables
        else "The program creates no data set, so the notebook writes no table.",
    ]
    if result.warnings:
        lines += ["", "**Review before running:**", ""]
        lines += [_warning(warning) for warning in result.warnings]
    return lines


def _written(result: ConversionResult, tables: list[tuple[str, str]]) -> str:
    names = ", ".join(code_span(table) for _, table in tables)
    if not result.datasets:
        return f"the result as the table {names}"
    if len(tables) == 1:
        return f"the data set {names} as a table of the same name"
    return f"the data sets {names} as tables of the same names"


def _location_cell(
    catalog: str, schema: str, output_catalog: str, output_schema: str
) -> str:
    """Widgets that choose the catalog and schema tables are read from, and
    those results are written to.

    The names go to the catalog API as values, never into SQL text, so any
    name works and none can change the statement. An empty widget keeps the
    current catalog or schema.
    """
    widgets = [
        ("catalog", catalog),
        ("schema", schema),
        ("output_catalog", output_catalog),
        ("output_schema", output_schema),
    ]
    return "\n".join(
        [
            *(
                f"dbutils.widgets.text({python_string(name)}, {python_string(value)})"
                for name, value in widgets
            ),
            'if dbutils.widgets.get("catalog"):',
            '    spark.catalog.setCurrentCatalog(dbutils.widgets.get("catalog"))',
            'if dbutils.widgets.get("schema"):',
            '    spark.catalog.setCurrentDatabase(dbutils.widgets.get("schema"))',
        ]
    )


def _write_cell(code: str, tables: list[tuple[str, str]]) -> str:
    """A function that replaces a table in the output catalog and schema, and
    a call for each result, run when the ``output_schema`` widget is set.

    The table name is quoted part by part, so the widgets' values are names,
    whatever characters they contain.
    """
    save = _unused_name("save_table", code)
    return "\n".join(
        [
            f"def {save}(dataframe, name):",
            '    """Replace the table ``name`` in the output catalog and schema."""',
            '    catalog = dbutils.widgets.get("output_catalog")',
            "    parts = [",
            "        catalog or spark.catalog.currentCatalog(),",
            '        dbutils.widgets.get("output_schema"),',
            "        name,",
            "    ]",
            '    table = ".".join("`" + part.replace("`", "``") + "`" '
            "for part in parts)",
            '    writer = dataframe.write.mode("overwrite")',
            '    writer.option("overwriteSchema", "true").saveAsTable(table)',
            "",
            "",
            'if dbutils.widgets.get("output_schema"):',
            *(
                f"    {save}({variable}, {python_string(table)})"
                for variable, table in tables
            ),
        ]
    )


def _unused_name(name: str, code: str) -> str:
    """``name``, or ``name_2``, ``name_3``, ... if the code already uses it."""
    used = set(re.findall(r"[A-Za-z_]\w*", code))
    candidate, number = name, 2
    while candidate in used:
        candidate, number = f"{name}_{number}", number + 1
    return candidate


def _warning(warning: Diagnostic) -> str:
    text = f"- {warning.message}: {code_span(warning.sql)}"
    if warning.hint:
        text += f". {warning.hint}"
    return text


def code_span(text: str) -> str:
    """Text as Markdown code, with enough backticks around it."""
    fence = "``" if "`" in text else "`"
    padding = " " if fence == "``" else ""
    return f"{fence}{padding}{text}{padding}{fence}"


def markdown_cell(lines: list[str]) -> str:
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
