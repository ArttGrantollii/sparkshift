"""Tests for writing generated code as a Databricks notebook."""

import ast

import pytest

import sparkshift
from sparkshift.diagnostics import ConversionResult, Diagnostic
from sparkshift.notebook import CELL_SEPARATOR, HEADER, to_notebook

SEPARATOR = f"\n\n{CELL_SEPARATOR}\n\n"


def cells(notebook: str) -> list[str]:
    """The notebook's cells, without the header line."""
    assert notebook.startswith(HEADER + "\n")
    assert notebook.endswith("\n")
    return notebook[len(HEADER) + 1 : -1].split(SEPARATOR)


def test_a_notebook_in_databricks_source_format() -> None:
    result = sparkshift.convert("SELECT name FROM customers WHERE score > 3")

    notebook = to_notebook(result, source="queries/good_customers.sql")

    assert notebook == (
        "# Databricks notebook source\n"
        "# MAGIC %md\n"
        "# MAGIC # good_customers\n"
        "# MAGIC\n"
        f"# MAGIC Converted from `good_customers.sql` (generic SQL) by SparkShift "
        f"{sparkshift.__version__}. Tables are read by name from the current "
        "catalog and schema: run `USE CATALOG` and `USE SCHEMA` first if they are "
        "elsewhere.\n"
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

    code_cells = cells(to_notebook(result))[1:-1]

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
        f"{sparkshift.__version__}. Tables are read by name from the current "
        "catalog and schema: run `USE CATALOG` and `USE SCHEMA` first if they are "
        "elsewhere.",
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
        f"{sparkshift.__version__}. Tables are read by name from the current "
        "catalog and schema: run `USE CATALOG` and `USE SCHEMA` first if they are "
        "elsewhere.",
    ]
