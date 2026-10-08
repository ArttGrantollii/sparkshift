import ast
import itertools
from decimal import Decimal

import pytest

from sparkshift import ir
from sparkshift.emit import emit, emit_expression, python_string, spark_identifier
from sparkshift.ir import (
    Alias,
    BinaryOp,
    BinaryOperator,
    Column,
    Literal,
    Project,
    Star,
    TableScan,
    UnaryOp,
    UnaryOperator,
)

A, B, C = Column(("a",)), Column(("b",)), Column(("c",))

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


def test_projection_golden_output() -> None:
    plan = Project(
        TableScan(("orders",)),
        (
            Column(("order_id",)),
            Alias(
                BinaryOp(BinaryOperator.MULTIPLY, Column(("amount",)), Literal(2)),
                "doubled",
            ),
        ),
    )

    assert emit(plan) == (
        "from pyspark.sql import functions as F\n"
        "\n"
        "result = (\n"
        '    spark.table("orders")\n'
        "    .select(\n"
        '        F.col("order_id"),\n'
        '        (F.col("amount") * F.lit(2)).alias("doubled"),\n'
        "    )\n"
        ")\n"
    )


def test_single_short_argument_stays_on_one_line() -> None:
    plan = Project(TableScan(("customers",)), (Column(("name",)),))

    assert emit(plan) == (
        "from pyspark.sql import functions as F\n"
        "\n"
        "result = (\n"
        '    spark.table("customers")\n'
        '    .select(F.col("name"))\n'
        ")\n"
    )


def test_long_single_argument_moves_to_its_own_line() -> None:
    long_name = "a_rather_long_column_name_that_does_not_fit_on_one_line_with_the_call"
    plan = Project(TableScan(("t",)), (Column((long_name,)),))

    assert f'    .select(\n        F.col("{long_name}"),\n    )' in emit(plan)


def test_decimal_literals_import_decimal() -> None:
    plan = Project(TableScan(("t",)), (Alias(Literal(Decimal("1.10")), "rate"),))

    assert emit(plan) == (
        "from decimal import Decimal\n"
        "\n"
        "from pyspark.sql import functions as F\n"
        "\n"
        "result = (\n"
        '    spark.table("t")\n'
        '    .select(F.lit(Decimal("1.10")).alias("rate"))\n'
        ")\n"
    )


def test_star_is_selected_by_name() -> None:
    plan = Project(TableScan(("t",)), (Star(), A))

    assert '.select(\n        "*",\n        F.col("a"),\n    )' in emit(plan)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "F.lit(None)"),
        (True, "F.lit(True)"),
        (30, "F.lit(30)"),
        (-5, "F.lit(-5)"),
        (Decimal("1.50"), 'F.lit(Decimal("1.50"))'),
        (1.5, "F.lit(1.5)"),
        ('say "hi"', 'F.lit("say \\"hi\\"")'),
    ],
)
def test_literal_code(value: ir.LiteralValue, expected: str) -> None:
    assert emit_expression(Literal(value)) == expected


# --- Parentheses -------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (
            # & binds tighter than == in Python, so comparisons need parentheses.
            BinaryOp(
                BinaryOperator.AND,
                BinaryOp(BinaryOperator.EQUAL, A, Literal(1)),
                BinaryOp(BinaryOperator.EQUAL, B, Literal(2)),
            ),
            '(F.col("a") == F.lit(1)) & (F.col("b") == F.lit(2))',
        ),
        (
            # & inside | is grouped for readability, though Python would not need it.
            BinaryOp(
                BinaryOperator.OR,
                BinaryOp(BinaryOperator.AND, A, B),
                UnaryOp(UnaryOperator.NOT, C),
            ),
            '(F.col("a") & F.col("b")) | ~F.col("c")',
        ),
        (
            UnaryOp(UnaryOperator.NOT, BinaryOp(BinaryOperator.GREATER, A, Literal(3))),
            '~(F.col("a") > F.lit(3))',
        ),
        (
            BinaryOp(
                BinaryOperator.GREATER, BinaryOp(BinaryOperator.ADD, A, B), Literal(0)
            ),
            'F.col("a") + F.col("b") > F.lit(0)',
        ),
        (
            BinaryOp(
                BinaryOperator.SUBTRACT, A, BinaryOp(BinaryOperator.SUBTRACT, B, C)
            ),
            'F.col("a") - (F.col("b") - F.col("c"))',
        ),
        (
            BinaryOp(
                BinaryOperator.SUBTRACT, BinaryOp(BinaryOperator.SUBTRACT, A, B), C
            ),
            'F.col("a") - F.col("b") - F.col("c")',
        ),
    ],
)
def test_parentheses_golden_output(expression: ir.Expression, expected: str) -> None:
    assert emit_expression(expression) == expected


_PYTHON_BINARY = {
    ast.Add: BinaryOperator.ADD,
    ast.Sub: BinaryOperator.SUBTRACT,
    ast.Mult: BinaryOperator.MULTIPLY,
    ast.Div: BinaryOperator.DIVIDE,
    ast.Mod: BinaryOperator.MODULO,
    ast.BitAnd: BinaryOperator.AND,
    ast.BitOr: BinaryOperator.OR,
}
_PYTHON_COMPARISON = {
    ast.Eq: BinaryOperator.EQUAL,
    ast.NotEq: BinaryOperator.NOT_EQUAL,
    ast.Lt: BinaryOperator.LESS,
    ast.LtE: BinaryOperator.LESS_EQUAL,
    ast.Gt: BinaryOperator.GREATER,
    ast.GtE: BinaryOperator.GREATER_EQUAL,
}
_PYTHON_UNARY = {ast.USub: UnaryOperator.NEGATE, ast.Invert: UnaryOperator.NOT}


def from_python(node: ast.expr) -> ir.Expression:
    """Rebuild IR from the tree Python's own parser builds for generated code."""
    match node:
        case ast.BinOp(left=left, op=op, right=right):
            return BinaryOp(
                _PYTHON_BINARY[type(op)], from_python(left), from_python(right)
            )
        case ast.Compare(left=left, ops=[op], comparators=[right]):
            return BinaryOp(
                _PYTHON_COMPARISON[type(op)], from_python(left), from_python(right)
            )
        case ast.UnaryOp(op=op, operand=operand):
            return UnaryOp(_PYTHON_UNARY[type(op)], from_python(operand))
        case ast.Call(func=ast.Attribute(attr="col"), args=[ast.Constant(value=name)]):
            return Column((name,))
    # Chained comparisons such as "a == b == c" also end up here.
    raise AssertionError(f"unexpected Python structure: {ast.dump(node)}")


def round_trip(expression: ir.Expression) -> ir.Expression:
    code = emit_expression(expression)
    return from_python(ast.parse(code, mode="eval").body)


def test_every_operator_pair_keeps_its_structure_in_python() -> None:
    # For each pair of operators, nest one inside the other on both sides and
    # under a unary operator. Python's parser must read the generated code back
    # as exactly the same tree; a missing parenthesis would change the shape.
    failures = []
    for outer, inner in itertools.product(BinaryOperator, repeat=2):
        nested = BinaryOp(inner, B, C)
        for expression in (
            BinaryOp(outer, nested, A),
            BinaryOp(outer, A, nested),
        ):
            if round_trip(expression) != expression:
                failures.append(emit_expression(expression))
    for unary, binary in itertools.product(UnaryOperator, BinaryOperator):
        for expression in (
            UnaryOp(unary, BinaryOp(binary, A, B)),
            BinaryOp(binary, UnaryOp(unary, A), B),
            BinaryOp(binary, A, UnaryOp(unary, B)),
        ):
            if round_trip(expression) != expression:
                failures.append(emit_expression(expression))

    assert failures == []


# --- Identifiers and strings -------------------------------------------------


def test_generated_code_is_valid_python() -> None:
    code = emit(TableScan(('we"ird', "na`me", "with\nnewline")))

    ast.parse(code)  # raises SyntaxError if the code is not valid Python


def test_emitter_rejects_unknown_ir_nodes() -> None:
    with pytest.raises(AssertionError):
        emit("not an IR node")  # type: ignore[arg-type]


def test_emitter_rejects_unknown_expression_nodes() -> None:
    with pytest.raises(AssertionError):
        emit_expression("not an IR node")  # type: ignore[arg-type]


@pytest.mark.parametrize("node", [TableScan, Column])
def test_names_are_required(node: type) -> None:
    with pytest.raises(ValueError, match="at least one name part"):
        node(())


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
def test_spark_identifier_quotes_only_when_needed(
    parts: tuple[str, ...], expected: str
) -> None:
    assert spark_identifier(parts) == expected


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
