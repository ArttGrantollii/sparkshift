"""Tests for pipeline source files (sparkshift.pipelines) and the IR and
emitter support they need. tests/equivalence/test_pipeline_runs.py runs them."""

import ast
from pathlib import Path

import pytest

import sparkshift
from sparkshift import ir
from sparkshift.diagnostics import ConversionResult
from sparkshift.emit import PipelineDataset, emit_pipeline
from sparkshift.errors import UnsupportedSQLError
from sparkshift.pipelines import (
    create_pipeline_notebook,
    default_pipeline_name,
    pipeline_settings,
    pipeline_tables,
    to_pipeline,
)

EXAMPLES = Path(__file__).parents[1] / "examples"


def issues(error: pytest.ExceptionInfo[UnsupportedSQLError]) -> list[str]:
    return [issue.message for issue in error.value.issues]


# --- ir.map_relations ------------------------------------------------------------


def test_map_relations_reaches_every_relation_and_subquery() -> None:
    def scan(name: str) -> ir.TableScan:
        return ir.TableScan((name,))

    inner = ir.Named("cte", ir.Filter(scan("a"), ir.Exists(scan("b"))))
    plan = ir.Sort(
        ir.Join(
            inner,
            ir.Project(scan("c"), (ir.ScalarSubquery(scan("d")),)),
            ir.JoinKind.INNER,
            ir.InSubquery(ir.Column(("x",)), scan("e")),
        ),
        (ir.SortKey(ir.ScalarSubquery(scan("f")), descending=False, nulls_first=True),),
    )

    renamed = ir.map_relations(
        plan,
        lambda node: (
            ir.TableScan(("s", *node.name_parts))
            if isinstance(node, ir.TableScan)
            else None
        ),
    )

    def names(node: ir.Relation) -> list[tuple[str, ...]]:
        found: list[tuple[str, ...]] = []
        ir.map_relations(
            node,
            lambda n: (
                found.append(n.name_parts) if isinstance(n, ir.TableScan) else None
            ),
        )
        return found

    assert names(plan) == [("a",), ("b",), ("c",), ("d",), ("e",), ("f",)]
    assert names(renamed) == [(("s", name)) for (name,) in names(plan)]


def test_map_relations_replaces_a_node_without_entering_it() -> None:
    named = ir.Named("a", ir.TableScan(("t",)))
    plan = ir.Filter(named, ir.Literal(True))

    replaced = ir.map_relations(
        plan, lambda node: ir.TableScan(("a",)) if node == named else None
    )

    assert replaced == ir.Filter(ir.TableScan(("a",)), ir.Literal(True))


# --- emit_pipeline ----------------------------------------------------------------


def test_a_pipeline_file_golden_output() -> None:
    orders = ir.TableScan(("orders",))
    paid = ir.Filter(
        orders,
        ir.BinaryOp(
            ir.BinaryOperator.EQUAL, ir.Column(("status",)), ir.Literal("paid")
        ),
    )

    code = emit_pipeline(
        [
            PipelineDataset("paid_v1", paid, temporary=True),
            PipelineDataset("paid", ir.Distinct(ir.TableScan(("paid_v1",)))),
        ]
    )

    assert code == (
        "from pyspark import pipelines as dp\n"
        "from pyspark.sql import functions as F\n"
        "\n"
        "\n"
        '@dp.temporary_view(name="paid_v1")\n'
        "def paid_v1():\n"
        "    return (\n"
        '        spark.table("orders")\n'
        '        .where(F.col("status") == F.lit("paid"))\n'
        "    )\n"
        "\n"
        "\n"
        '@dp.materialized_view(name="paid")\n'
        "def paid():\n"
        "    return (\n"
        '        spark.table("paid_v1")\n'
        "        .distinct()\n"
        "    )\n"
    )


def test_pipeline_functions_have_their_own_names() -> None:
    table = ir.TableScan(("t",))

    code = emit_pipeline(
        [
            PipelineDataset("dp", table),
            PipelineDataset("spark", table),
            PipelineDataset("my table", table),
            PipelineDataset("my_table", table),
        ]
    )

    functions = [
        node.name for node in ast.parse(code).body if isinstance(node, ast.FunctionDef)
    ]
    assert functions == ["dp_df", "spark_df", "my_table", "my_table_2"]
    assert '@dp.materialized_view(name="my table")' in code


def test_a_pipeline_file_needs_a_dataset() -> None:
    with pytest.raises(ValueError, match="at least one dataset"):
        emit_pipeline([])


def test_pipeline_functions_declare_what_they_use_inside() -> None:
    query = (
        "WITH totals AS (SELECT customer_id, SUM(amount) AS total FROM orders "
        "GROUP BY customer_id) SELECT c.name, t.total FROM customers c "
        "JOIN totals t ON c.customer_id = t.customer_id"
    )

    code = to_pipeline(sparkshift.convert(query), source="totals.sql")

    body = code.split("def totals():\n", 1)[1]
    assert body.startswith('    customers = spark.table("customers")\n')
    assert "    totals = (\n" in body
    assert "\n    return (\n" in body


# --- to_pipeline: SQL ---------------------------------------------------------------


def test_a_query_is_a_materialized_view_named_after_its_file() -> None:
    result = sparkshift.convert("SELECT name FROM customers WHERE score > 3")

    code = to_pipeline(result, source="reports/Top Customers.sql")

    assert '@dp.materialized_view(name="top_customers")\ndef top_customers():' in code
    assert 'spark.table("customers")' in code
    assert pipeline_tables(result, source="reports/Top Customers.sql") == [
        "top_customers"
    ]
    assert pipeline_tables(result) == ["result"]


@pytest.mark.parametrize(
    ("table", "catalog", "schema", "read"),
    [
        ("orders", "", "", "orders"),
        ("orders", "", "raw", "raw.orders"),
        ("orders", "main", "raw", "main.raw.orders"),
        ("sales.orders", "", "raw", "sales.orders"),
        ("sales.orders", "main", "raw", "main.sales.orders"),
        ("hive.sales.orders", "main", "raw", "hive.sales.orders"),
    ],
)
def test_tables_are_read_from_the_given_schema(
    table: str, catalog: str, schema: str, read: str
) -> None:
    result = sparkshift.convert(
        f"SELECT a FROM {table} WHERE a IN (SELECT a FROM {table})"
    )

    code = to_pipeline(result, source="q.sql", catalog=catalog, schema=schema)

    assert code.count(f'spark.table("{read}")') == 2


def test_a_query_that_reads_the_table_it_defines_is_rejected() -> None:
    result = sparkshift.convert("SELECT a FROM Orders")

    with pytest.raises(UnsupportedSQLError) as caught:
        to_pipeline(result, source="orders.sql")

    assert issues(caught) == [
        "a pipeline that defines orders and reads a table named orders"
    ]
    # Read from its schema, the table is another one.
    assert 'spark.table("raw.Orders")' in to_pipeline(
        result, source="orders.sql", schema="raw"
    )


def test_a_pipeline_needs_a_schema_for_its_catalog_and_a_converted_result() -> None:
    result = sparkshift.convert("SELECT a FROM t")

    with pytest.raises(ValueError, match="a catalog needs a schema"):
        to_pipeline(result, catalog="main")
    with pytest.raises(ValueError, match=r"returned by sparkshift\.convert"):
        to_pipeline(ConversionResult(code="result = spark.table('t')\n"))


def test_the_header_says_what_the_file_defines_and_lists_warnings() -> None:
    program = (EXAMPLES / "sas" / "customer_cleanup.sas").read_text(encoding="utf-8")
    result = sparkshift.convert(program, dialect="sas")

    code = to_pipeline(result, source="customer_cleanup.sas", dialect="sas")

    header, _ = code.split("\n\nimport datetime\n", 1)
    text = " ".join(line.removeprefix("#").strip() for line in header.splitlines())
    assert text.startswith(
        "Pipeline source converted from customer_cleanup.sas (SAS) by SparkShift"
    )
    assert (
        "It defines the materialized views customers_raw and customers_clean, and "
        "the temporary view customers_clean_v1, which only the pipeline reads."
    ) in text
    assert "Tables without a schema are read from the pipeline's default" in text
    assert "PROC PRINT shows a data set and creates none" in text
    assert "Review before running: - text truncated to the length of age_group" in text
    assert all(line.startswith("#") for line in header.splitlines())


def test_metadata_cannot_become_code() -> None:
    sql = 'SELECT a AS "x\n# COMMAND ----------\nimport os" FROM t'
    result = sparkshift.convert(sql)
    tricky = ConversionResult(
        code=result.code,
        warnings=(sparkshift.Diagnostic("line\nbreak", "a\nb", "c\nd"),),
        _plans=result._plans,
    )

    code = to_pipeline(tricky, source="q.sql")

    header = code.split("\n\nfrom pyspark", 1)[0]
    assert all(line.startswith("#") for line in header.splitlines())
    ast.parse(code)


# --- to_pipeline: SAS ---------------------------------------------------------------


def sas(program: str, **options: str) -> str:
    result = sparkshift.convert(program, dialect="sas")
    return to_pipeline(result, source="job.sas", dialect="sas", **options)


def test_each_data_set_is_a_materialized_view_reading_the_others_by_name() -> None:
    code = sas("data a; set t; x = 1; run; data b; set a; where x > 0; run;")

    assert '@dp.materialized_view(name="a")\ndef a():' in code
    assert '@dp.materialized_view(name="b")\ndef b():' in code
    body_b = code.split("def b():\n", 1)[1]
    assert 'spark.table("a")' in body_b
    assert 'spark.table("t")' not in body_b


def test_a_data_set_created_again_is_first_a_temporary_view() -> None:
    program = (
        "data a; set t; run; proc sort data=a; by x; run; "
        "data b; set a; run; proc sort data=a; by y; run;"
    )
    result = sparkshift.convert(program, dialect="sas")

    code = to_pipeline(result, source="job.sas", dialect="sas")

    assert pipeline_tables(result, dialect="sas") == ["a_v1", "a_v2", "b", "a"]
    assert '@dp.temporary_view(name="a_v1")' in code
    assert '@dp.temporary_view(name="a_v2")' in code
    assert 'spark.table("a_v2")' in code.split("def b():\n", 1)[1]
    assert 'spark.table("a_v2")' in code.split("def a():\n", 1)[1]


def test_version_names_never_take_a_data_set_s_name() -> None:
    program = "data a_v1; set t; run; data a; set t; run; proc sort data=a; by x; run;"

    result = sparkshift.convert(program, dialect="sas")

    assert pipeline_tables(result, dialect="sas") == ["a_v1", "a_v1_2", "a"]


def test_equal_versions_of_a_data_set_are_one_dataset() -> None:
    program = "data a; set t; run; data a; set t; run;"

    result = sparkshift.convert(program, dialect="sas")

    assert pipeline_tables(result, dialect="sas") == ["a"]


def test_a_program_that_only_prints_cannot_be_a_pipeline() -> None:
    result = sparkshift.convert("proc print data=t; run;", dialect="sas")

    with pytest.raises(UnsupportedSQLError) as caught:
        to_pipeline(result, source="report.sas", dialect="sas")

    assert issues(caught) == ["a pipeline of a program that creates no data set"]


def test_a_data_set_named_like_a_table_it_reads_is_rejected() -> None:
    program = "data summary; set orders; run; data orders; set summary; run;"

    with pytest.raises(UnsupportedSQLError) as caught:
        sas(program)

    assert issues(caught) == [
        "a pipeline that defines orders and reads a table named orders"
    ]
    assert 'spark.table("raw.orders")' in sas(program, schema="raw")


@pytest.mark.parametrize(
    ("program", "dialect"),
    [
        (
            "data b; set t; if x > 3 then do; x = 0; y = y + 1; end; "
            "else do; x = 1; y = y + 2; end; run;",
            "sas",
        ),
        (
            "SELECT * FROM t QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1",
            "snowflake",
        ),
    ],
)
def test_helper_names_chosen_when_the_code_runs_cannot_be_in_a_pipeline(
    program: str, dialect: str
) -> None:
    result = sparkshift.convert(program, dialect=dialect)
    assert ".columns" in result.code  # the plain code looks at them

    with pytest.raises(UnsupportedSQLError) as caught:
        to_pipeline(result, source="job.sql", dialect=dialect)

    assert issues(caught) == [
        "a helper column named after the input's columns, in a pipeline"
    ]


def test_a_helper_named_now_is_fine_in_a_pipeline() -> None:
    program = (
        "data b; set t(keep=x y); if x > 3 then do; x = 0; y = y + 1; end; "
        "else do; x = 1; y = y + 2; end; run;"
    )

    code = sas(program)

    assert ".columns" not in code
    assert '.withColumn("_if_1"' in code


def test_rows_written_in_a_pipeline_have_a_typed_schema() -> None:
    code = sas(
        "data a; input id name $ day :date9.; datalines;\n1 Ann 01JAN2024\n;\nrun;"
    )

    assert "from pyspark.sql import types as T\n" in code
    assert (
        "        T.StructType(\n"
        "            [\n"
        '                T.StructField("id", T.DoubleType()),\n'
        '                T.StructField("name", T.StringType()),\n'
        '                T.StructField("day", T.DateType()),\n'
        "            ]\n"
        "        ),\n"
    ) in code
    # Plain code keeps the schema as text.
    plain = sparkshift.convert(
        "data a; input id; datalines;\n1\n;\nrun;", dialect="sas"
    ).code
    assert '"`id` double",' in plain


def test_permanent_data_sets_keep_their_library() -> None:
    code = sas("data a; set sales.orders; run;", catalog="main", schema="raw")

    assert 'spark.table("main.sales.orders")' in code


# --- Every example as a pipeline ----------------------------------------------------


EXAMPLE_FILES = sorted((EXAMPLES / "sql").rglob("*.sql")) + sorted(
    (EXAMPLES / "sas").glob("*.sas")
)


@pytest.mark.parametrize("path", EXAMPLE_FILES, ids=lambda path: path.name)
def test_every_example_is_a_pipeline_of_python_that_fits_the_line_length(
    path: Path,
) -> None:
    dialect = "sas" if path.suffix == ".sas" else path.parent.name
    dialect = None if dialect == "generic" else dialect
    result = sparkshift.convert(path.read_text(encoding="utf-8"), dialect=dialect)

    code = to_pipeline(result, source=path.name, dialect=dialect, schema="raw")

    ast.parse(code)
    code_lines = [line for line in code.splitlines() if not line.startswith("#")]
    assert [line for line in code_lines if len(line) > 88] == []


# --- Deploying ---------------------------------------------------------------------

FOLDER = "/Workspace/Users/me@example.com/migrated"


def test_pipeline_settings_are_the_create_body() -> None:
    files = [f"{FOLDER}/load.py", f"{FOLDER}/report/summary.py"]

    settings = pipeline_settings(
        "nightly", FOLDER, files, catalog="main", schema="claims"
    )

    assert settings == {
        "name": "nightly",
        "catalog": "main",
        "schema": "claims",
        "serverless": True,
        "continuous": False,
        "libraries": [{"file": {"path": path}} for path in files],
        "root_path": FOLDER,
        "tags": {"sparkshift": sparkshift.__version__},
    }
    assert default_pipeline_name(FOLDER) == "sparkshift migrated"


def test_the_create_pipeline_notebook_sends_the_same_settings() -> None:
    settings = pipeline_settings(
        "nightly", FOLDER, [f"{FOLDER}/load.py"], catalog="main", schema="claims"
    )

    notebook = create_pipeline_notebook(settings)

    assert notebook.startswith("# Databricks notebook source\n# MAGIC %md\n")
    tree = ast.parse(notebook)
    [assignment] = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign) and node.targets[0].id == "settings"  # type: ignore[attr-defined]
    ]
    assert ast.literal_eval(assignment.value) == settings
    assert 'path = "/api/2.0/pipelines"' in notebook
    assert 'client.api_client.do("POST", path, body=settings)' in notebook
    assert 'path = f"/api/2.0/pipelines/{pipeline_id}"' in notebook
    assert "# MAGIC 1. `/Workspace/Users/me@example.com/migrated/load.py`" in notebook


def test_dataset_names_need_no_check() -> None:
    # Names come before the checks, which depend on where tables are read:
    # this query is a valid pipeline when it reads orders from a schema.
    result = sparkshift.convert("SELECT a FROM orders")

    assert pipeline_tables(result, source="orders.sql") == ["orders"]
    assert 'spark.table("raw.orders")' in to_pipeline(
        result, source="orders.sql", schema="raw"
    )


def test_a_repeated_version_keeps_its_first_name() -> None:
    # The first two versions are equal: one dataset, named once.
    program = "data a; set t; run; data a; set t; run; proc sort data=a; by x; run;"

    result = sparkshift.convert(program, dialect="sas")

    assert pipeline_tables(result, dialect="sas") == ["a_v1", "a"]
