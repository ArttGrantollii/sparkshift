"""Generated helper columns must preserve user data, even without a schema."""

import ast
from typing import TYPE_CHECKING

import pytest
from compare import differences, snapshot
from harness import assert_equivalent, run_generated

import sparkshift

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark


@pytest.fixture
def helper_tables(spark: "SparkSession") -> "SparkSession":
    schema = (
        "id int, amount double, _if_1 int, _if_1_ int, _IF_1__ int, "
        "_qualify_1 int, _having_1 int, _order_1 int, _having int"
    )
    rows = [
        (1, 10.0, 777, 111, 222, 31, 41, 51, 61),
        (2, -1.0, 888, 112, 223, 32, 42, 52, 62),
        (3, 20.0, 999, 113, 224, 33, 43, 53, 63),
    ]
    spark.createDataFrame(rows, schema).createOrReplaceTempView("helper_inputs")
    spark.createDataFrame(rows, schema).drop("_qualify_1").createOrReplaceTempView(
        "helper_other"
    )
    return spark


@pytest.mark.parametrize("case_sensitive", [False, True])
def test_sas_if_preserves_unknown_input_columns(
    helper_tables: "SparkSession", case_sensitive: bool
) -> None:
    spark = helper_tables
    setting = "spark.sql.caseSensitive"
    previous = spark.conf.get(setting)
    try:
        spark.conf.set(setting, str(case_sensitive).lower())
        program = (
            "data output; set helper_inputs; "
            "if amount > 0 then do; amount=0; id=id+1; end; "
            "else do; amount=1; id=id+2; end; run;"
        )
        code = sparkshift.convert(program, dialect="sas").code
        actual = snapshot(run_generated(spark, code))
        expected = snapshot(
            spark.sql(
                "SELECT CASE WHEN amount > 0 THEN id+1 ELSE id+2 END AS id, "
                "CAST(CASE WHEN amount > 0 THEN 0 ELSE 1 END AS DOUBLE) AS amount, "
                "_if_1, _if_1_, `_IF_1__`, _qualify_1, _having_1, _order_1, _having "
                "FROM helper_inputs"
            )
        )
        assert not differences(expected, actual), (expected, actual, code)
    finally:
        spark.conf.set(setting, previous)


def test_helper_cannot_overwrite_a_variable_assigned_in_its_branches(
    helper_tables: "SparkSession",
) -> None:
    program = (
        "data output; set helper_inputs(keep=id amount); "
        "if amount > 0 then do; amount=0; _if_1=100; end; "
        "else do; amount=1; _if_1=200; end; run;"
    )
    code = sparkshift.convert(program, dialect="sas").code
    actual = snapshot(run_generated(helper_tables, code))
    expected = snapshot(
        helper_tables.sql(
            "SELECT id, CAST(CASE WHEN amount > 0 THEN 0 ELSE 1 END AS DOUBLE) "
            "AS amount, "
            "CASE WHEN amount > 0 THEN 100 ELSE 200 END AS _if_1 FROM helper_inputs"
        )
    )
    assert not differences(expected, actual), (expected, actual, code)


@pytest.mark.parametrize("nested", [False, True])
def test_sas_if_with_known_columns_names_its_helper_statically(
    helper_tables: "SparkSession", nested: bool
) -> None:
    # KEEP= lists every input column, so the helper's name is chosen during
    # conversion: no schema lookup in the generated code, and _if_1 survives.
    inner = "if id > 1 then do; id=id+10; amount=amount+1; end; " if nested else ""
    program = (
        "data output; set helper_inputs(keep=id amount _if_1 _IF_1__); "
        f"if amount > 0 then do; amount=0; {inner}id=id+1; end; "
        "else do; amount=1; id=id+2; end; run;"
    )
    code = sparkshift.convert(program, dialect="sas").code
    assert ".columns" not in code
    assert "while" not in code
    actual = snapshot(run_generated(helper_tables, code))
    nested_id = "CASE WHEN id > 1 THEN id + 11 ELSE id + 1 END" if nested else "id+1"
    nested_amount = "CASE WHEN id > 1 THEN 1 ELSE 0 END" if nested else "0"
    expected = snapshot(
        helper_tables.sql(
            f"SELECT CASE WHEN amount > 0 THEN {nested_id} ELSE id+2 END AS id, "
            f"CAST(CASE WHEN amount > 0 THEN {nested_amount} ELSE 1 END AS DOUBLE) "
            "AS amount, _if_1, `_IF_1__` FROM helper_inputs"
        )
    )
    assert not differences(expected, actual), (expected, actual, code)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM helper_inputs QUALIFY ROW_NUMBER() OVER (ORDER BY id)=1",
        "SELECT t.* FROM helper_inputs t QUALIFY ROW_NUMBER() OVER (ORDER BY id)=1",
        "SELECT *, id AS _QUALIFY_1_ FROM helper_inputs "
        "QUALIFY ROW_NUMBER() OVER (ORDER BY id)=1",
        "SELECT t.*, id AS _qualify_2 FROM helper_inputs t "
        "QUALIFY ROW_NUMBER() OVER (ORDER BY id)=1 AND t.amount > 0",
        "SELECT id AS _qualify_1 FROM helper_inputs "
        "QUALIFY ROW_NUMBER() OVER (ORDER BY id)=1",
        "SELECT id AS _having_1 FROM helper_inputs GROUP BY id HAVING SUM(amount)>0",
        "SELECT id, COUNT(*) AS _order_1 FROM helper_inputs "
        "GROUP BY id ORDER BY SUM(amount)",
        "WITH a AS (SELECT * FROM helper_inputs "
        "QUALIFY ROW_NUMBER() OVER (ORDER BY id)=1), "
        "b AS (SELECT * FROM helper_other "
        "QUALIFY ROW_NUMBER() OVER (ORDER BY id)=1) "
        "SELECT a._qualify_1, b.id FROM a JOIN b ON a.id=b.id",
    ],
)
def test_sql_helpers_do_not_collide_with_columns_or_aliases(
    helper_tables: "SparkSession", sql: str
) -> None:
    keys = ["id", "_order_1"] if "ORDER BY SUM" in sql else None
    assert_equivalent(helper_tables, sql, order_keys=keys)


def test_proc_sql_helper_reserves_unselected_group_keys(
    helper_tables: "SparkSession",
) -> None:
    query = "SELECT id FROM helper_inputs GROUP BY id, _having HAVING SUM(amount)>0"
    program = f"proc sql; create table output as {query}; quit;"
    code = sparkshift.convert(program, dialect="sas").code
    expected = snapshot(helper_tables.sql(query))
    actual = snapshot(run_generated(helper_tables, code))
    assert not differences(expected, actual), (expected, actual, code)


def test_generated_bindings_still_wrap_without_changing_python_syntax() -> None:
    from sparkshift.emit import emit
    from sparkshift.parsing import parse_sql
    from sparkshift.translate import translate

    plan = translate(
        parse_sql(
            "SELECT * FROM helper_inputs QUALIFY ROW_NUMBER() OVER (ORDER BY id)=1"
        )
    )
    wide, normal, narrow = (emit(plan, line_length=w) for w in (10000, 88, 30))
    assert ast.dump(ast.parse(wide)) == ast.dump(ast.parse(normal))
    assert ast.dump(ast.parse(normal)) == ast.dump(ast.parse(narrow))
    assert all(len(line) <= 88 for line in normal.splitlines())


def test_nested_if_helpers_preserve_outer_conditions_and_user_columns(
    helper_tables: "SparkSession",
) -> None:
    program = (
        "data output; set helper_inputs; if id > 1 then do; id=id+10; "
        "if amount > 0 then do; amount=0; id=id+1; end; "
        "else do; amount=2; id=id+2; end; end; run;"
    )
    code = sparkshift.convert(program, dialect="sas").code
    actual = snapshot(run_generated(helper_tables, code))
    expected = snapshot(
        helper_tables.sql(
            "SELECT CASE WHEN id > 1 THEN id + CASE WHEN amount > 0 "
            "THEN 11 ELSE 12 END "
            "ELSE id END AS id, CASE WHEN id > 1 THEN CASE WHEN amount > 0 THEN 0 "
            "ELSE 2 END ELSE amount END AS amount, _if_1, _if_1_, `_IF_1__`, "
            "_qualify_1, _having_1, _order_1, _having FROM helper_inputs"
        )
    )
    assert not differences(expected, actual), (expected, actual, code)


@pytest.mark.parametrize("name", ["_\u0131f_1", "_\u0130f_1"])
def test_unicode_case_variants_of_helper_names_are_preserved(
    helper_tables: "SparkSession", name: str
) -> None:
    helper_tables.sql(
        f"SELECT id, amount, 777 AS `{name}` FROM helper_inputs"
    ).createOrReplaceTempView("helper_unicode")
    program = (
        "data output; set helper_unicode; if amount > 0 then do; "
        "amount=0; id=id+1; end; else do; amount=1; id=id+2; end; run;"
    )
    code = sparkshift.convert(program, dialect="sas").code
    actual = snapshot(run_generated(helper_tables, code))
    expected = snapshot(
        helper_tables.sql(
            "SELECT CASE WHEN amount > 0 THEN id+1 ELSE id+2 END AS id, "
            "CAST(CASE WHEN amount > 0 THEN 0 ELSE 1 END AS DOUBLE) AS amount, "
            f"777 AS `{name}` FROM helper_inputs"
        )
    )
    assert not differences(expected, actual), (expected, actual, code)


@pytest.mark.parametrize("suffix", ["", " ORDER BY id LIMIT 1", " LIMIT 0"])
@pytest.mark.parametrize("negation", ["", "NOT "])
@pytest.mark.parametrize("distinct", ["", "DISTINCT "])
def test_correlated_query_can_allocate_helpers_without_resolving_outer_columns(
    helper_tables: "SparkSession", suffix: str, negation: str, distinct: str
) -> None:
    sql = (
        f"SELECT h.id FROM helper_inputs h WHERE {negation}EXISTS "
        f"(SELECT {distinct}* FROM helper_other o WHERE o.id=h.id "
        f"QUALIFY ROW_NUMBER() OVER (ORDER BY o.id)=1{suffix})"
    )
    assert_equivalent(helper_tables, sql)
