import ast

import pytest

from sparkshift.emit import emit, python_string, spark_table_name
from sparkshift.ir import TableScan

# --- Golden output: the exact code users will see ----------------------------


@pytest.mark.parametrize(
    ("plan", "expected_code"),
    [
        (TableScan(("customers",)), 'result = spark.table("customers")\n'),
        (
            TableScan(("sales", "customers")),
            'result = spark.table("sales.customers")\n',
        ),
        (TableScan(("my table",)), 'result = spark.table("`my table`")\n'),
    ],
)
def test_table_scan_golden_output(plan: TableScan, expected_code: str) -> None:
    assert emit(plan) == expected_code


def test_generated_code_is_valid_python() -> None:
    code = emit(TableScan(('we"ird', "na`me", "with\nnewline")))

    ast.parse(code)  # raises SyntaxError if the code is not valid Python


def test_emitter_rejects_unknown_ir_nodes() -> None:
    with pytest.raises(AssertionError):
        emit("not an IR node")  # type: ignore[arg-type]


def test_table_scan_requires_a_name() -> None:
    with pytest.raises(ValueError, match="at least one name part"):
        TableScan(())


# --- Spark identifiers -------------------------------------------------------


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (("customers",), "customers"),
        (("main", "sales", "customers"), "main.sales.customers"),
        (("_private", "t2"), "_private.t2"),
        (("my table",), "`my table`"),
        (("2024_orders",), "`2024_orders`"),
        (("sales", "order-items"), "sales.`order-items`"),
        (("we`ird",), "`we``ird`"),
    ],
)
def test_spark_table_name_quotes_only_when_needed(
    parts: tuple[str, ...], expected: str
) -> None:
    assert spark_table_name(parts) == expected


# --- Python string literals --------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        'say "hi"',
        "it's",
        "back\\slash",
        "new\nline",
        "tab\there",
        "both ' and \"",
        "café",
        "nul\x00byte",
        "",
    ],
)
def test_python_string_round_trips(value: str) -> None:
    literal = python_string(value)

    assert literal.startswith('"')
    assert ast.literal_eval(literal) == value
