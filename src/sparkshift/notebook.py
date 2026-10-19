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
"""

import re
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
    tables = _tables(result, source)
    cells = [markdown_cell(_description(result, source, dialect, tables))]
    cells.append(_location_cell(catalog, schema, output_catalog, output_schema))
    cells += _code_cells(result.code)
    cells.append(_write_cell(result.code, tables))
    cells.append("display(result)")
    body = f"\n\n{CELL_SEPARATOR}\n\n".join(cells)
    return f"{HEADER}\n{body}\n"


def _tables(result: ConversionResult, source: str | None) -> list[tuple[str, str]]:
    """The variable and table name of each result the notebook can write.

    A SAS program's data sets keep their names; a program that creates a
    data set twice writes the second, as SAS replaces the first. A SQL
    query's result is named after its file, or ``result`` without one.
    """
    if not result.datasets:
        return [("result", _table_name(source))]
    seen: set[str] = set()
    last: list[tuple[str, str]] = []
    for dataset in reversed(result.datasets):
        if dataset.name not in seen:
            seen.add(dataset.name)
            last.append((dataset.variable, dataset.name))
    return last[::-1]


def _table_name(source: str | None) -> str:
    """A table name from a file name: its stem in lowercase, with characters
    other than ASCII letters, digits and underscores replaced by underscores."""
    stem = PurePath(source).stem if source else ""
    return re.sub(r"[^a-z0-9_]", "_", stem.lower()) or "result"


def _description(
    result: ConversionResult,
    source: str | None,
    dialect: str | None,
    tables: list[tuple[str, str]],
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
        "`output_schema` empty to only display the result.",
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
