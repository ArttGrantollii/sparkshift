"""Tests for writing generated code as a Databricks notebook."""

import ast

import pytest

import sparkshift
from sparkshift.diagnostics import ConversionResult, Diagnostic
from sparkshift.notebook import (
    CELL_SEPARATOR,
    HEADER,
    Cell,
    markdown_text,
    notebook_cells,
    to_notebook,
)
from sparkshift.notebook import _table_name as table_name

SEPARATOR = f"\n\n{CELL_SEPARATOR}\n\n"


def cells(notebook: str) -> list[str]:
    """The notebook's cells, without the header line."""
    assert notebook.startswith(HEADER + "\n")
    assert notebook.endswith("\n")
    return notebook[len(HEADER) + 1 : -1].split(SEPARATOR)


def location_cell(
    catalog: str = "",
    schema: str = "",
    output_catalog: str = "",
    output_schema: str = "",
) -> str:
    """The cell whose widgets choose where tables are read from and where
    results are written."""
    return (
        f'dbutils.widgets.text("catalog", "{catalog}")\n'
        f'dbutils.widgets.text("schema", "{schema}")\n'
        f'dbutils.widgets.text("output_catalog", "{output_catalog}")\n'
        f'dbutils.widgets.text("output_schema", "{output_schema}")\n'
        'if dbutils.widgets.get("catalog"):\n'
        '    spark.catalog.setCurrentCatalog(dbutils.widgets.get("catalog"))\n'
        'if dbutils.widgets.get("schema"):\n'
        '    spark.catalog.setCurrentDatabase(dbutils.widgets.get("schema"))'
    )


def write_cell(*tables: tuple[str, str], save: str = "save_table") -> str:
    """The cell that writes each (variable, table) when output_schema is set."""
    calls = "".join(
        f'\n    {save}({variable}, "{table}")' for variable, table in tables
    )
    return (
        f"def {save}(dataframe, name):\n"
        '    """Replace the table ``name`` in the output catalog and schema."""\n'
        '    catalog = dbutils.widgets.get("output_catalog")\n'
        "    parts = [\n"
        "        catalog or spark.catalog.currentCatalog(),\n"
        '        dbutils.widgets.get("output_schema"),\n'
        "        name,\n"
        "    ]\n"
        '    table = ".".join("`" + part.replace("`", "``") + "`" for part in parts)\n'
        '    writer = dataframe.write.mode("overwrite")\n'
        '    writer.option("overwriteSchema", "true").saveAsTable(table)\n'
        "\n"
        "\n"
        'if dbutils.widgets.get("output_schema"):' + calls
    )


def program(code: str, table: str = "result") -> str:
    """What a notebook runs: the widget cell, the code, the write cell, and
    display(result)."""
    return (
        f"{location_cell()}\n{code}\n{write_cell(('result', table))}\ndisplay(result)\n"
    )


def writing(what: str) -> str:
    """The description's paragraph on writing results."""
    return (
        f"# MAGIC Set the `output_schema` widget to save {what} in that schema, in "
        "the catalog in the `output_catalog` widget or the current one. Tables of "
        "the same name are replaced. Leave `output_schema` empty to only display "
        "the result."
    )


def test_a_notebook_in_databricks_source_format() -> None:
    result = sparkshift.convert("SELECT name FROM customers WHERE score > 3")

    notebook = to_notebook(result, source="queries/good_customers.sql")

    assert notebook == (
        "# Databricks notebook source\n"
        "# MAGIC %md\n"
        "# MAGIC # good_customers\n"
        "# MAGIC\n"
        f"# MAGIC Converted from `good_customers.sql` (generic SQL) by SparkShift "
        f"{sparkshift.__version__}. Tables are read by name from the catalog and "
        "schema in the `catalog` and `schema` widgets at the top of the notebook; "
        "leave a widget empty to use the current one.\n"
        "# MAGIC\n"
        "# MAGIC Needs a PySpark 4 client: on serverless compute, use environment "
        "version 4 or later.\n"
        "# MAGIC\n"
        f"{writing('the result as the table `good_customers`')}\n"
        "\n"
        "# COMMAND ----------\n"
        "\n"
        f"{location_cell()}\n"
        "\n"
        "# COMMAND ----------\n"
        "\n"
        "from pyspark.sql import functions as F\n"
        "\n"
        "# COMMAND ----------\n"
        "\n"
        "result = (\n"
        '    spark.table("customers")\n'
        '    .where(F.col("score") > F.lit(3))\n'
        '    .select(F.col("name"))\n'
        ")\n"
        "\n"
        "# COMMAND ----------\n"
        "\n"
        f"{write_cell(('result', 'good_customers'))}\n"
        "\n"
        "# COMMAND ----------\n"
        "\n"
        "display(result)\n"
    )


def test_each_section_of_the_code_is_a_cell() -> None:
    result = sparkshift.convert(
        "WITH big AS (SELECT * FROM orders WHERE amount > 100) "
        "SELECT o.order_id, c.name, "
        "LEAD(o.amount, 1, 0.5) OVER (ORDER BY o.order_id) AS next_amount, "
        "ROW_NUMBER() OVER (PARTITION BY c.name ORDER BY o.order_id) AS n "
        "FROM big o JOIN customers c ON o.customer_id = c.customer_id"
    )

    code_cells = cells(to_notebook(result))[2:-2]

    # The import sections share a cell; the others keep the code's sections.
    sections = result.code.strip("\n").split("\n\n")
    imports = [s for s in sections if s.startswith(("from ", "import "))]
    assert len(imports) == 2
    assert code_cells[0] == "\n".join(imports)
    assert code_cells[1:] == sections[len(imports) :]
    # Together, the cells are the same program as the code.
    together = ast.parse("\n\n".join(code_cells))
    assert ast.dump(together) == ast.dump(ast.parse(result.code))


def test_a_notebook_is_python() -> None:
    result = sparkshift.convert("SELECT name FROM customers")

    tree = ast.parse(to_notebook(result))

    assert ast.unparse(tree.body[-1]) == "display(result)"


def test_warnings_are_listed_for_review() -> None:
    result = ConversionResult(
        "result = spark.table('t')\n",
        (
            Diagnostic("KEEP of several variables", "keep=a b", "List them in order."),
            Diagnostic("equality of two columns", "a.x = `b`.x"),
        ),
    )

    description = cells(to_notebook(result, source="job.sas", dialect="sas"))[0]

    assert description.splitlines()[3:] == [
        "# MAGIC Converted from `job.sas` (SAS) by SparkShift "
        f"{sparkshift.__version__}. Tables are read by name from the catalog and "
        "schema in the `catalog` and `schema` widgets at the top of the notebook; "
        "leave a widget empty to use the current one.",
        "# MAGIC",
        "# MAGIC Needs a PySpark 4 client: on serverless compute, use environment "
        "version 4 or later.",
        "# MAGIC",
        # A SAS program that only prints creates no data set to write.
        "# MAGIC The program creates no data set, so the notebook writes no table.",
        "# MAGIC",
        "# MAGIC **Review before running:**",
        "# MAGIC",
        "# MAGIC - KEEP of several variables: `keep=a b`. List them in order.",
        "# MAGIC - equality of two columns: `` a.x = `b`.x ``",
    ]


@pytest.mark.parametrize(
    ("dialect", "language"),
    [
        (None, "generic SQL"),
        ("tsql", "T-SQL"),
        ("TSQL", "T-SQL"),
        ("postgres", "PostgreSQL"),
        ("mysql", "MySQL"),
        ("snowflake", "Snowflake"),
        ("bigquery", "BigQuery"),
        ("oracle", "Oracle"),
        ("SAS", "SAS"),
    ],
)
def test_the_language_is_named(dialect: str | None, language: str) -> None:
    result = ConversionResult("result = spark.table('t')\n")

    description = cells(to_notebook(result, source="q.sql", dialect=dialect))[0]

    assert f"Converted from `q.sql` ({language})" in description


def test_without_a_source() -> None:
    result = ConversionResult("result = spark.table('t')\n")

    description = cells(to_notebook(result))[0]

    assert description.splitlines()[:4] == [
        "# MAGIC %md",
        "# MAGIC # Converted code",
        "# MAGIC",
        "# MAGIC Converted from code (generic SQL) by SparkShift "
        f"{sparkshift.__version__}. Tables are read by name from the catalog and "
        "schema in the `catalog` and `schema` widgets at the top of the notebook; "
        "leave a widget empty to use the current one.",
    ]


def test_a_sas_program_that_only_prints_writes_no_table() -> None:
    result = sparkshift.convert("proc print data=customers; run;", dialect="sas")

    notebook = to_notebook(result, source="report.sas", dialect="sas")

    assert result.datasets == ()
    assert "save_table" not in notebook
    assert cells(notebook)[-1] == "display(result)"
    assert "writes no table" in cells(notebook)[0]


def test_multiline_sas_warning_remains_notebook_metadata() -> None:
    result = sparkshift.convert(
        "proc sort data=customers out=unique_customers nodupkey;\n"
        "by customer_id;\nrun;",
        dialect="sas",
    )
    assert result.warnings
    notebook = to_notebook(result, source="deduplicate.sas", dialect="sas")
    expected = program(result.code).replace(
        'save_table(result, "result")',
        'save_table(unique_customers, "unique_customers")',
    )
    assert ast.dump(ast.parse(notebook)) == ast.dump(ast.parse(expected))
    assert all(line.startswith("# MAGIC") for line in cells(notebook)[0].splitlines())


@pytest.mark.parametrize("newline", ["\n", "\r", "\r\n"])
@pytest.mark.parametrize("field", ["source", "dialect", "message", "sql", "hint"])
def test_metadata_cannot_create_executable_code_or_notebook_cells(
    newline: str, field: str
) -> None:
    payload = newline.join(
        ["metadata", "# COMMAND ----------", "raise RuntimeError('injected')"]
    )
    values = {"message": "warning", "sql": "fragment", "hint": "guidance"}
    options = {"source": "query.sql", "dialect": "tsql"}
    if field in values:
        values[field] = payload
    else:
        options[field] = payload
    result = ConversionResult("result = spark.table('t')\n", (Diagnostic(**values),))
    notebook = to_notebook(result, **options)
    table = table_name(options["source"])
    assert ast.dump(ast.parse(notebook)) == ast.dump(
        ast.parse(program(result.code, table))
    )
    assert notebook.splitlines().count(CELL_SEPARATOR) == 4
    assert all(line.startswith("# MAGIC") for line in cells(notebook)[0].splitlines())


def test_widgets_start_with_the_given_catalog_and_schema() -> None:
    result = ConversionResult("result = spark.table('t')\n")

    notebook = to_notebook(
        result,
        catalog="workspace",
        schema="raw",
        output_catalog="main",
        output_schema="claims",
    )

    assert cells(notebook)[1] == location_cell("workspace", "raw", "main", "claims")


@pytest.mark.parametrize(
    ("source", "table"),
    [
        ("queries/good_customers.sql", "good_customers"),
        ("Daily Revenue.sql", "daily_revenue"),
        ("2024-q1.v2.sql", "2024_q1_v2"),
        ("Zoë.sql", "zo_"),
        (None, "result"),
    ],
)
def test_a_query_result_is_written_to_a_table_named_after_its_file(
    source: str | None, table: str
) -> None:
    result = ConversionResult("result = spark.table('t')\n")

    notebook_cells = cells(to_notebook(result, source=source))

    assert notebook_cells[-2] == write_cell(("result", table))
    assert f"the result as the table `{table}`" in notebook_cells[0]


def test_each_data_set_of_a_sas_program_is_written() -> None:
    program_text = (
        "data big; set orders; where amount > 100; run;\n"
        "proc sort data=big out=sorted; by amount; run;\n"
        # The same name again: SAS replaces the data set, so only this is written.
        "data big; set sorted; where amount > 200; run;\n"
        # A name the code itself uses: its variable has another name.
        "data result; set big; run;\n"
    )
    result = sparkshift.convert(program_text, dialect="sas")

    notebook_cells = cells(to_notebook(result, source="job.sas", dialect="sas"))

    assert [(d.name, d.variable) for d in result.datasets] == [
        ("big", "big"),
        ("sorted", "sorted"),
        ("big", "big_2"),
        ("result", "result_df"),
    ]
    assert notebook_cells[-2] == write_cell(
        ("sorted", "sorted"), ("big_2", "big"), ("result_df", "result")
    )
    assert (
        "the data sets `sorted`, `big`, `result` as tables of the same names"
        in notebook_cells[0]
    )


def test_one_data_set_is_named_in_the_singular() -> None:
    result = sparkshift.convert("data big; set orders; run;", dialect="sas")

    description = cells(to_notebook(result, dialect="sas"))[0]

    assert "the data set `big` as a table of the same name in" in description


def test_the_writer_does_not_replace_a_name_the_code_uses() -> None:
    result = ConversionResult(
        "save_table = spark.table('a')\n"
        "save_table_2 = save_table\n"
        "result = save_table\n"
    )

    notebook_cells = cells(to_notebook(result))

    assert notebook_cells[-2] == write_cell(("result", "result"), save="save_table_3")


@pytest.mark.parametrize(
    "name",
    [
        'main"; import os; "',
        "back\\slash",
        "line\nbreak",
        f"x\n{CELL_SEPARATOR}\nraise RuntimeError('injected')",
        "`odd name`",
        "Zoë",
    ],
)
def test_any_catalog_or_schema_name_stays_a_plain_value(name: str) -> None:
    """A name is a Python string value, never code: whatever it contains, the
    widget's default is exactly that name and the program is unchanged."""
    result = ConversionResult("result = spark.table('t')\n")

    notebook = to_notebook(
        result, catalog=name, schema=name, output_catalog=name, output_schema=name
    )

    tree = ast.parse(notebook)
    defaults = [
        node.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func) == "dbutils.widgets.text"
    ]
    assert defaults == [name] * 4
    assert len(tree.body) == len(ast.parse(program(result.code)).body)
    assert notebook.splitlines().count(CELL_SEPARATOR) == 4


# --- Cells, for pages that show them -------------------------------------------


def test_the_cells_make_the_notebook() -> None:
    program = "data a; set t; x = 1; run; data b; set a; run;"
    result = sparkshift.convert(program, dialect="sas")
    options = {"source": "job.sas", "dialect": "sas", "output_schema": "out"}

    cells = notebook_cells(result, **options)

    notebook = to_notebook(result, **options)
    separator = f"\n\n{CELL_SEPARATOR}\n\n"
    assert notebook == f"{HEADER}\n{separator.join(cell.source for cell in cells)}\n"
    assert [cell.kind for cell in cells] == [
        "markdown",
        "setup",
        "code",  # the imports
        "code",  # a
        "code",  # b
        "code",  # result = b
        "setup",
        "code",  # display(result)
    ]
    assert [cell.title for cell in cells if cell.kind == "setup"] == [
        "Widgets: the catalog and schema to read from and write to",
        "Write the results to tables",
    ]
    assert all(cell.title == "" for cell in cells if cell.kind != "setup")
    assert cells[-1] == Cell("code", "display(result)")


def test_a_notebook_without_tables_has_one_setup_cell() -> None:
    result = sparkshift.convert("proc print data=t; run;", dialect="sas")

    kinds = [cell.kind for cell in notebook_cells(result, dialect="sas")]

    assert kinds.count("setup") == 1


def test_markdown_text_is_the_markdown_of_a_cell() -> None:
    result = sparkshift.convert("SELECT a FROM t")
    [markdown] = [cell for cell in notebook_cells(result) if cell.kind == "markdown"]

    text = markdown_text(markdown.source)

    assert text.startswith("# Converted code\n\nConverted from code (generic SQL)")
    assert "# MAGIC" not in text
    cell = "# MAGIC %md\n# MAGIC # Title\n# MAGIC\n# MAGIC   kept"
    assert markdown_text(cell) == "# Title\n\n  kept"
