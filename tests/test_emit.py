import ast
import itertools
from decimal import Decimal

import pytest

from sparkshift import ir
from sparkshift.emit import (
    emit,
    emit_expression,
    python_string,
    spark_identifier,
    table_variables,
)
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

    # A single argument gets no trailing comma, as Black formats it.
    assert f'    .select(\n        F.col("{long_name}")\n    )' in emit(plan)


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


# --- Chains ------------------------------------------------------------------


def test_full_chain_golden_output() -> None:
    plan = ir.Limit(
        ir.Distinct(
            Project(
                ir.Filter(
                    TableScan(("customers",)),
                    BinaryOp(
                        BinaryOperator.AND,
                        Column(("is_active",)),
                        BinaryOp(
                            BinaryOperator.GREATER, Column(("score",)), Literal(2)
                        ),
                    ),
                ),
                (Column(("country",)),),
            )
        ),
        10,
    )

    assert emit(plan) == (
        "from pyspark.sql import functions as F\n"
        "\n"
        "result = (\n"
        '    spark.table("customers")\n'
        '    .where(F.col("is_active") & (F.col("score") > F.lit(2)))\n'
        '    .select(F.col("country"))\n'
        "    .distinct()\n"
        "    .limit(10)\n"
        ")\n"
    )


def test_chain_without_column_expressions_needs_no_import() -> None:
    plan = ir.Limit(ir.Distinct(TableScan(("t",))), 5)

    assert emit(plan) == (
        'result = (\n    spark.table("t")\n    .distinct()\n    .limit(5)\n)\n'
    )


def test_negative_limit_is_rejected_by_the_ir() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        ir.Limit(TableScan(("t",)), -1)


# --- Joins -------------------------------------------------------------------

CUSTOMERS = TableScan(("customers",))
ORDERS = TableScan(("orders",))
KEYS_MATCH = BinaryOp(
    BinaryOperator.EQUAL, Column(("c", "customer_id")), Column(("o", "customer_id"))
)


def test_join_golden_output() -> None:
    plan = Project(
        ir.Join(
            ir.RelationAlias(CUSTOMERS, "c"),
            ir.RelationAlias(ORDERS, "o"),
            ir.JoinKind.INNER,
            KEYS_MATCH,
        ),
        (Column(("c", "country")),),
    )

    assert emit(plan) == (
        "from pyspark.sql import functions as F\n"
        "\n"
        'customers = spark.table("customers")\n'
        'orders = spark.table("orders")\n'
        "\n"
        "result = (\n"
        '    customers.alias("c")\n'
        "    .join(\n"
        '        orders.alias("o"),\n'
        '        F.col("c.customer_id") == F.col("o.customer_id"),\n'
        '        "inner",\n'
        "    )\n"
        '    .select(F.col("c.country"))\n'
        ")\n"
    )


def test_self_join_declares_the_table_once() -> None:
    plan = ir.Join(
        ir.RelationAlias(ORDERS, "o1"),
        ir.RelationAlias(ORDERS, "o2"),
        ir.JoinKind.CROSS,
    )

    code = emit(plan)

    assert code.count('spark.table("orders")') == 1
    assert '    orders.alias("o1")\n    .crossJoin(orders.alias("o2"))\n' in code


def test_using_join_and_redundant_aliases() -> None:
    # A table used once, aliased to its own simple name, needs no .alias().
    plan = ir.Join(
        ir.RelationAlias(CUSTOMERS, "customers"),
        ir.RelationAlias(ORDERS, "orders"),
        ir.JoinKind.FULL,
        using=("customer_id",),
    )

    assert emit(plan) == (
        'customers = spark.table("customers")\n'
        'orders = spark.table("orders")\n'
        "\n"
        "result = (\n"
        "    customers\n"
        "    .join(\n"
        "        orders,\n"
        '        ["customer_id"],\n'
        '        "full",\n'
        "    )\n"
        ")\n"
    )


def test_schema_qualified_table_keeps_its_alias() -> None:
    plan = ir.Join(
        ir.RelationAlias(TableScan(("sales", "customers")), "customers"),
        ir.RelationAlias(ORDERS, "orders"),
        ir.JoinKind.CROSS,
    )

    assert '    customers.alias("customers")\n' in emit(plan)


def test_single_table_alias_stays_inline() -> None:
    plan = Project(ir.RelationAlias(CUSTOMERS, "c"), (Column(("c", "name")),))

    assert '    spark.table("customers").alias("c")\n' in emit(plan)


def test_qualified_star_code() -> None:
    assert emit_expression(Star(("c",))) == 'F.col("c.*")'
    assert emit_expression(Star(("my table",))) == 'F.col("`my table`.*")'


@pytest.mark.parametrize(
    ("tables", "expected"),
    [
        ([("customers",), ("orders",)], ["customers", "orders"]),
        (
            [("sales", "customers"), ("crm", "customers")],
            ["customers", "crm_customers"],
        ),
        ([("2024_orders",), ("order-items",)], ["t_2024_orders", "order_items"]),
        ([("class",), ("result",), ("spark",)], ["class_df", "result_df", "spark_df"]),
        ([("a", "x"), ("b", "x"), ("a_x",)], ["x", "b_x", "a_x"]),
        ([("a", "x"), ("a_x",), ("b", "x"), ("b_x",)], ["x", "a_x", "b_x", "b_x_2"]),
    ],
)
def test_table_variable_names(
    tables: list[tuple[str, ...]], expected: list[str]
) -> None:
    plan: ir.Relation = TableScan(tables[0])
    for parts in tables[1:]:
        plan = ir.Join(plan, TableScan(parts), ir.JoinKind.CROSS)

    names = table_variables(plan)

    assert list(names.values()) == expected
    for name in names.values():
        assert name.isidentifier()


@pytest.mark.parametrize(
    "arguments",
    [
        {"kind": ir.JoinKind.CROSS, "condition": KEYS_MATCH},
        {"kind": ir.JoinKind.CROSS, "using": ("id",)},
        {"kind": ir.JoinKind.INNER},
        {"kind": ir.JoinKind.LEFT, "condition": KEYS_MATCH, "using": ("id",)},
    ],
)
def test_join_ir_rejects_inconsistent_conditions(arguments: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ir.Join(CUSTOMERS, ORDERS, **arguments)  # type: ignore[arg-type]


def test_alias_after_other_operations_is_a_chain_step() -> None:
    plan = ir.RelationAlias(ir.Distinct(TableScan(("t",))), "d")

    assert emit(plan) == (
        'result = (\n    spark.table("t")\n    .distinct()\n    .alias("d")\n)\n'
    )


def test_table_variable_numbering_skips_taken_names() -> None:
    plan = ir.Join(
        ir.Join(TableScan(("b_x_2",)), TableScan(("a", "b_x")), ir.JoinKind.CROSS),
        TableScan(("b_x",)),
        ir.JoinKind.CROSS,
    )

    assert list(table_variables(plan).values()) == ["b_x_2", "b_x", "b_x_3"]


# --- Aggregation -------------------------------------------------------------

COUNT_ROWS = ir.AggregateCall(ir.AggregateFunction.COUNT)


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        (COUNT_ROWS, "F.count(F.lit(1))"),
        (ir.AggregateCall(ir.AggregateFunction.COUNT, (A,)), 'F.count(F.col("a"))'),
        (
            ir.AggregateCall(ir.AggregateFunction.COUNT, (A, B), True),
            'F.count_distinct(F.col("a"), F.col("b"))',
        ),
        (ir.AggregateCall(ir.AggregateFunction.SUM, (A,)), 'F.sum(F.col("a"))'),
        (
            ir.AggregateCall(ir.AggregateFunction.SUM, (A,), True),
            'F.sum_distinct(F.col("a"))',
        ),
        (ir.AggregateCall(ir.AggregateFunction.AVG, (A,)), 'F.avg(F.col("a"))'),
        (ir.AggregateCall(ir.AggregateFunction.MIN, (A,)), 'F.min(F.col("a"))'),
        (ir.AggregateCall(ir.AggregateFunction.MAX, (A,)), 'F.max(F.col("a"))'),
    ],
)
def test_aggregate_call_code(call: ir.AggregateCall, expected: str) -> None:
    assert emit_expression(call) == expected


def test_aggregate_expression_parenthesizes_as_a_unit() -> None:
    doubled = BinaryOp(BinaryOperator.MULTIPLY, COUNT_ROWS, Literal(2))

    expected = '(F.count(F.lit(1)) * F.lit(2)).alias("x")'
    assert emit_expression(Alias(doubled, "x")) == expected


def test_grouped_aggregation_golden_output() -> None:
    plan = ir.Aggregate(
        TableScan(("orders",)),
        (Column(("status",)),),
        (Alias(COUNT_ROWS, "n"), ir.AggregateCall(ir.AggregateFunction.SUM, (A,))),
    )

    assert emit(plan) == (
        "from pyspark.sql import functions as F\n"
        "\n"
        "result = (\n"
        '    spark.table("orders")\n'
        '    .groupBy(F.col("status"))\n'
        "    .agg(\n"
        '        F.count(F.lit(1)).alias("n"),\n'
        '        F.sum(F.col("a")),\n'
        "    )\n"
        ")\n"
    )


def test_global_aggregation_selects_the_aggregates() -> None:
    # select(...) of aggregates is one row for the whole input, like agg(...),
    # and also works in subqueries that refer to their enclosing query.
    plan = ir.Aggregate(TableScan(("orders",)), (), (COUNT_ROWS,))

    assert '    spark.table("orders")\n    .select(F.count(F.lit(1)))\n' in emit(plan)


def test_grouping_without_aggregates_selects_distinct_keys() -> None:
    plan = ir.Aggregate(TableScan(("orders",)), (Column(("status",)),), ())

    assert '    .select(F.col("status"))\n    .distinct()\n' in emit(plan)


def test_aggregate_ir_needs_keys_or_aggregates() -> None:
    with pytest.raises(ValueError, match="keys or aggregates"):
        ir.Aggregate(TableScan(("t",)), (), ())


# --- Predicates, conditionals, and casts -------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (
            ir.InList(A, (Literal(1), Literal(None))),
            'F.col("a").isin(F.lit(1), F.lit(None))',
        ),
        (
            UnaryOp(UnaryOperator.NOT, ir.InList(A, (Literal(1),))),
            '~F.col("a").isin(F.lit(1))',
        ),
        (ir.Between(A, Literal(1), B), 'F.col("a").between(F.lit(1), F.col("b"))'),
        (ir.Like(A, 'x"%'), 'F.col("a").like("x\\"%")'),
        (ir.Like(A, "x%", case_insensitive=True), 'F.col("a").ilike("x%")'),
        (ir.IsNull(A), 'F.col("a").isNull()'),
        (ir.IsNull(A, negated=True), 'F.col("a").isNotNull()'),
        (ir.NullSafeEqual(A, B), 'F.col("a").eqNullSafe(F.col("b"))'),
        (
            ir.FunctionCall("coalesce", (A, Literal(0))),
            'F.coalesce(F.col("a"), F.lit(0))',
        ),
        (ir.Cast(A, "decimal(10,2)"), 'F.col("a").cast("decimal(10,2)")'),
        (ir.Cast(A, "int", safe=True), 'F.col("a").try_cast("int")'),
        (
            # A compound receiver is parenthesized before the method call.
            ir.Cast(BinaryOp(BinaryOperator.ADD, A, B), "int"),
            '(F.col("a") + F.col("b")).cast("int")',
        ),
        (
            ir.Case(((A, Literal(1)), (B, Literal(2))), Literal(0)),
            'F.when(F.col("a"), F.lit(1))'
            '.when(F.col("b"), F.lit(2))'
            ".otherwise(F.lit(0))",
        ),
        (ir.Case(((A, Literal(1)),)), 'F.when(F.col("a"), F.lit(1))'),
    ],
)
def test_predicate_and_conversion_code(
    expression: ir.Expression, expected: str
) -> None:
    assert emit_expression(expression) == expected


def test_method_results_combine_with_python_operators() -> None:
    expression = BinaryOp(
        BinaryOperator.AND,
        UnaryOp(UnaryOperator.NOT, ir.InList(A, (Literal(1),))),
        ir.IsNull(B, negated=True),
    )

    code = emit_expression(expression)

    assert code == '~F.col("a").isin(F.lit(1)) & F.col("b").isNotNull()'
    # Python reads "~x.isin(...)" as "~(x.isin(...))", as intended.
    ast.parse(code, mode="eval")


def test_case_needs_a_branch() -> None:
    with pytest.raises(ValueError, match="at least one branch"):
        ir.Case(())


def test_generic_children_cover_every_expression_field() -> None:
    case = ir.Case(((A, Literal(1)),), B)

    assert ir.children(case) == [A, Literal(1), B]
    assert ir.map_children(case, lambda child: C) == ir.Case(((C, C),), C)


# --- String and numeric functions --------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (ir.FunctionCall("upper", (A,)), 'F.upper(F.col("a"))'),
        # Parameters PySpark takes as plain values are emitted as plain values.
        (ir.FunctionCall("round", (A, Literal(2))), 'F.round(F.col("a"), 2)'),
        (
            ir.FunctionCall("substring", (A, Literal(2), Literal(3))),
            'F.substring(F.col("a"), 2, 3)',
        ),
        (
            ir.FunctionCall("concat_ws", (Literal(""), A, B)),
            'F.concat_ws("", F.col("a"), F.col("b"))',
        ),
        (ir.FunctionCall("log", (Literal(10.0), A)), 'F.log(10.0, F.col("a"))'),
        # Everywhere else, values stay Columns.
        (ir.FunctionCall("left", (A, Literal(2))), 'F.left(F.col("a"), F.lit(2))'),
        (
            ir.FunctionCall("replace", (A, Literal("x"), Literal("y"))),
            'F.replace(F.col("a"), F.lit("x"), F.lit("y"))',
        ),
        (ir.FunctionCall("substr", (A, Literal(2))), 'F.substr(F.col("a"), F.lit(2))'),
    ],
)
def test_function_code(expression: ir.Expression, expected: str) -> None:
    assert emit_expression(expression) == expected


# --- Dates and timestamps ----------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (ir.Interval("days", Literal(3)), "F.make_interval(days=F.lit(3))"),
        (ir.Interval("months", A), 'F.make_interval(months=F.col("a"))'),
        (
            BinaryOp(BinaryOperator.ADD, A, ir.Interval("weeks", Literal(2))),
            'F.col("a") + F.make_interval(weeks=F.lit(2))',
        ),
        (
            ir.FunctionCall("date_trunc", (Literal("month"), A)),
            'F.date_trunc("month", F.col("a"))',
        ),
        (ir.FunctionCall("trunc", (A, Literal("year"))), 'F.trunc(F.col("a"), "year")'),
        (ir.FunctionCall("current_date", ()), "F.current_date()"),
        (ir.FunctionCall("datediff", (A, B)), 'F.datediff(F.col("a"), F.col("b"))'),
    ],
)
def test_date_code(expression: ir.Expression, expected: str) -> None:
    assert emit_expression(expression) == expected


def test_interval_rejects_unknown_units() -> None:
    with pytest.raises(ValueError, match="Unsupported interval unit"):
        ir.Interval("hours", Literal(1))


# --- Wrapping long expressions -----------------------------------------------


def _condition(name: str, value: str) -> ir.Expression:
    return BinaryOp(BinaryOperator.EQUAL, Column((name,)), Literal(value))


LONG_AND = BinaryOp(
    BinaryOperator.AND,
    BinaryOp(
        BinaryOperator.AND,
        _condition("status", "completed"),
        _condition("country", "Spain"),
    ),
    _condition("channel", "online"),
)


def test_long_condition_wraps_one_condition_per_line() -> None:
    code = emit(ir.Filter(TableScan(("orders",)), LONG_AND))

    assert code.endswith(
        "    .where(\n"
        '        (F.col("status") == F.lit("completed"))\n'
        '        & (F.col("country") == F.lit("Spain"))\n'
        '        & (F.col("channel") == F.lit("online"))\n'
        "    )\n"
        ")\n"
    )


def test_long_case_wraps_one_branch_per_line() -> None:
    case = ir.Case(
        (
            (_condition("status", "completed"), Literal("done")),
            (_condition("status", "pending"), Literal("waiting")),
        ),
        Literal("other"),
    )
    plan = Project(TableScan(("orders",)), (Alias(case, "state"),))

    assert (
        "    .select(\n"
        '        F.when(F.col("status") == F.lit("completed"), F.lit("done"))\n'
        '        .when(F.col("status") == F.lit("pending"), F.lit("waiting"))\n'
        '        .otherwise(F.lit("other"))\n'
        '        .alias("state")\n'
        "    )\n"
    ) in emit(plan)


def test_long_function_call_wraps_one_argument_per_line() -> None:
    names = ("first_name", "middle_name", "last_name", "street", "city")
    call_ = ir.FunctionCall("concat_ws", (Literal(" "), *(Column((n,)) for n in names)))
    plan = Project(TableScan(("people",)), (Alias(call_, "label"),))

    assert (
        "        F.concat_ws(\n"
        '            " ",\n'
        '            F.col("first_name"),\n'
        '            F.col("middle_name"),\n'
        '            F.col("last_name"),\n'
        '            F.col("street"),\n'
        '            F.col("city"),\n'
        '        ).alias("label")\n'
    ) in emit(plan)


def test_nested_boolean_chains_get_their_own_parentheses() -> None:
    expression = BinaryOp(BinaryOperator.OR, LONG_AND, _condition("vip", "yes"))
    plan = ir.Filter(TableScan(("orders",)), expression)

    code = emit(plan)

    assert (
        "    .where(\n"
        "        (\n"
        '            (F.col("status") == F.lit("completed"))\n'
        '            & (F.col("country") == F.lit("Spain"))\n'
        '            & (F.col("channel") == F.lit("online"))\n'
        "        )\n"
        '        | (F.col("vip") == F.lit("yes"))\n'
        "    )\n"
    ) in code


def test_short_expressions_are_not_wrapped() -> None:
    plan = ir.Filter(TableScan(("t",)), _condition("a", "x"))

    assert '    .where(F.col("a") == F.lit("x"))\n' in emit(plan)


@pytest.mark.parametrize("line_length", [88, 40, 10])
def test_wrapping_never_changes_the_python_syntax_tree(line_length: int) -> None:
    # Every operator pairing from the precedence round trip, inside a long
    # boolean chain and a long function call, at several widths.
    expressions = []
    for outer, inner in itertools.product(BinaryOperator, repeat=2):
        expressions.append(BinaryOp(outer, BinaryOp(inner, B, C), A))
        expressions.append(BinaryOp(outer, A, BinaryOp(inner, B, C)))
    filler = [_condition(f"column_{i}", "value") for i in range(3)]
    for expression in expressions:
        condition = BinaryOp(BinaryOperator.AND, filler[0], expression)
        condition = BinaryOp(BinaryOperator.OR, condition, filler[1])
        call_ = ir.FunctionCall("coalesce", (expression, *filler))
        plan = Project(ir.Filter(TableScan(("t",)), condition), (Alias(call_, "x"),))

        wrapped = emit(plan, line_length=line_length)
        one_line = emit(plan, line_length=10**6)

        assert ast.dump(ast.parse(wrapped)) == ast.dump(ast.parse(one_line))


def test_long_aliased_arithmetic_moves_into_its_own_parentheses() -> None:
    total = BinaryOp(
        BinaryOperator.ADD,
        Column(("shipping_cost_in_euros",)),
        Column(("handling_fee_in_euros",)),
    )
    plan = Project(TableScan(("orders",)), (Alias(total, "total_extra_cost"),))

    assert (
        "        (\n"
        '            F.col("shipping_cost_in_euros") + F.col("handling_fee_in_euros")\n'
        '        ).alias("total_extra_cost")\n'
    ) in emit(plan)


def test_long_negated_between_wraps_its_bounds() -> None:
    between = ir.Between(
        Column(("order_date",)),
        ir.Cast(Literal("2024-01-10"), "date"),
        ir.Cast(Literal("2024-02-29"), "date"),
    )
    negated = UnaryOp(UnaryOperator.NOT, between)
    plan = Project(TableScan(("orders",)), (Alias(negated, "outside"),))

    assert (
        "        (\n"
        '            ~F.col("order_date").between(\n'
        '                F.lit("2024-01-10").cast("date"),\n'
        '                F.lit("2024-02-29").cast("date"),\n'
        "            )\n"
        '        ).alias("outside")\n'
    ) in emit(plan)


@pytest.mark.parametrize("line_length", [88, 30, 10])
def test_wrapping_negations_keeps_the_python_syntax_tree(line_length: int) -> None:
    negated_chain = UnaryOp(UnaryOperator.NOT, LONG_AND)
    negated_call = UnaryOp(UnaryOperator.NEGATE, ir.FunctionCall("greatest", (A, B, C)))
    plan = Project(
        ir.Filter(TableScan(("t",)), negated_chain),
        (Alias(negated_call, "x"), Alias(negated_chain, "y")),
    )

    wrapped = emit(plan, line_length=line_length)
    one_line = emit(plan, line_length=10**6)

    assert ast.dump(ast.parse(wrapped)) == ast.dump(ast.parse(one_line))


# --- Sorting -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("descending", "nulls_first", "method"),
    [
        (False, True, "asc"),  # Spark's default for ascending
        (False, False, "asc_nulls_last"),
        (True, False, "desc"),  # Spark's default for descending
        (True, True, "desc_nulls_first"),
    ],
)
def test_sort_key_spells_out_only_non_default_null_placement(
    descending: bool, nulls_first: bool, method: str
) -> None:
    key = ir.SortKey(A, descending, nulls_first)

    assert emit(ir.Sort(TableScan(("t",)), (key,))).endswith(
        f'    .orderBy(F.col("a").{method}())\n)\n'
    )


def test_several_sort_keys_go_one_per_line() -> None:
    keys = (ir.SortKey(A, True, False), ir.SortKey(B, False, True))

    assert emit(ir.Sort(TableScan(("t",)), keys)).endswith(
        "    .orderBy(\n"
        '        F.col("a").desc(),\n'
        '        F.col("b").asc(),\n'
        "    )\n"
        ")\n"
    )


def test_sort_key_on_an_operator_expression_is_parenthesized() -> None:
    key = ir.SortKey(BinaryOp(BinaryOperator.ADD, A, B), False, False)

    assert '.orderBy((F.col("a") + F.col("b")).asc_nulls_last())' in emit(
        ir.Sort(TableScan(("t",)), (key,))
    )


def test_long_sort_key_wraps_like_an_alias() -> None:
    total = BinaryOp(
        BinaryOperator.ADD,
        Column(("shipping_cost_in_euros",)),
        Column(("handling_fee_in_euros",)),
    )
    plan = ir.Sort(TableScan(("orders",)), (ir.SortKey(total, True, True),))

    assert emit(plan).endswith(
        "    .orderBy(\n"
        "        (\n"
        '            F.col("shipping_cost_in_euros") + F.col("handling_fee_in_euros")\n'
        "        ).desc_nulls_first()\n"
        "    )\n"
        ")\n"
    )


@pytest.mark.parametrize("line_length", [88, 30, 10])
def test_wrapping_sort_keys_keeps_the_python_syntax_tree(line_length: int) -> None:
    keys = (
        ir.SortKey(LONG_AND, False, False),
        ir.SortKey(ir.FunctionCall("greatest", (A, B, C)), True, True),
        ir.SortKey(UnaryOp(UnaryOperator.NEGATE, A), False, True),
    )
    plan = ir.Sort(TableScan(("t",)), keys)

    wrapped = emit(plan, line_length=line_length)
    one_line = emit(plan, line_length=10**6)

    assert ast.dump(ast.parse(wrapped)) == ast.dump(ast.parse(one_line))


def test_sort_requires_keys() -> None:
    with pytest.raises(ValueError, match="at least one key"):
        ir.Sort(TableScan(("t",)), ())


# --- Window functions --------------------------------------------------------


def ranked(*partition_by: ir.Expression) -> ir.WindowCall:
    return ir.WindowCall(
        ir.WindowFunction.RANK, partition_by, (ir.SortKey(B, False, True),)
    )


def total(*partition_by: ir.Expression) -> ir.WindowCall:
    return ir.WindowCall(ir.AggregateCall(ir.AggregateFunction.SUM, (C,)), partition_by)


def test_a_window_is_declared_once_and_shared() -> None:
    running = ir.WindowCall(
        ir.AggregateCall(ir.AggregateFunction.SUM, (C,)),
        (A,),
        (ir.SortKey(B, False, True),),
    )
    plan = Project(
        TableScan(("t",)), (Alias(ranked(A), "r"), Alias(running, "running"))
    )

    assert emit(plan) == (
        "from pyspark.sql import Window\n"
        "from pyspark.sql import functions as F\n"
        "\n"
        'window = Window.partitionBy(F.col("a")).orderBy(F.col("b").asc())\n'
        "\n"
        "result = (\n"
        '    spark.table("t")\n'
        "    .select(\n"
        '        F.rank().over(window).alias("r"),\n'
        '        F.sum(F.col("c")).over(window).alias("running"),\n'
        "    )\n"
        ")\n"
    )


def test_different_windows_are_numbered_in_order_of_use() -> None:
    plan = Project(
        TableScan(("t",)),
        (Alias(total(), "everything"), Alias(total(A), "per_a"), Alias(ranked(), "r")),
    )

    code = emit(plan)

    assert (
        "window_1 = Window.partitionBy()\n"
        'window_2 = Window.partitionBy(F.col("a"))\n'
        'window_3 = Window.orderBy(F.col("b").asc())\n'
    ) in code
    assert 'F.sum(F.col("c")).over(window_1).alias("everything")' in code
    assert 'F.rank().over(window_3).alias("r")' in code


def test_long_window_wraps_one_call_per_line() -> None:
    columns = [Column((name,)) for name in ("country", "region", "city", "street")]
    call = ir.WindowCall(
        ir.WindowFunction.ROW_NUMBER,
        tuple(columns[:2]),
        tuple(ir.SortKey(column, True, False) for column in columns[2:]),
    )

    code = emit(Project(TableScan(("t",)), (Alias(call, "n"),)))

    assert (
        "window = (\n"
        "    Window.partitionBy(\n"
        '        F.col("country"),\n'
        '        F.col("region"),\n'
        "    )\n"
        "    .orderBy(\n"
        '        F.col("city").desc(),\n'
        '        F.col("street").desc(),\n'
        "    )\n"
        ")\n"
    ) in code


def test_window_variable_does_not_shadow_a_table_variable() -> None:
    join = ir.Join(
        ir.RelationAlias(TableScan(("window",)), "w"),
        ir.RelationAlias(TableScan(("t",)), "t"),
        ir.JoinKind.CROSS,
    )

    code = emit(Project(join, (Alias(total(), "s"),)))

    assert 'window = spark.table("window")\n' in code
    assert "window_spec = Window.partitionBy()\n" in code
    assert ".over(window_spec)" in code


def test_window_alone_is_written_inline() -> None:
    assert (
        emit_expression(ranked(A))
        == 'F.rank().over(Window.partitionBy(F.col("a")).orderBy(F.col("b").asc()))'
    )


def test_long_window_aggregate_wraps_like_an_alias() -> None:
    long_sum = ir.WindowCall(
        ir.AggregateCall(
            ir.AggregateFunction.SUM,
            (
                BinaryOp(
                    BinaryOperator.ADD,
                    Column(("shipping_cost_in_euros",)),
                    Column(("handling_fee_in_euros",)),
                ),
            ),
        )
    )

    code = emit(Project(TableScan(("t",)), (Alias(long_sum, "extra"),)))

    assert (
        "        F.sum(\n"
        '            F.col("shipping_cost_in_euros") + '
        'F.col("handling_fee_in_euros"),\n'
        '        ).over(window).alias("extra")\n'
    ) in code


@pytest.mark.parametrize("line_length", [88, 30, 10])
def test_wrapping_windows_keeps_the_python_syntax_tree(line_length: int) -> None:
    plan = Project(
        TableScan(("t",)),
        (
            Alias(ranked(A, B, C), "r"),
            Alias(total(LONG_AND), "s"),
            Alias(BinaryOp(BinaryOperator.ADD, total(A), Literal(1)), "plus_one"),
        ),
    )

    wrapped = emit(plan, line_length=line_length)
    one_line = emit(plan, line_length=10**6)

    assert ast.dump(ast.parse(wrapped)) == ast.dump(ast.parse(one_line))


def test_generic_children_include_window_keys() -> None:
    call = ir.WindowCall(COUNT_ROWS, (A,), (ir.SortKey(B, True, False),))

    assert ir.children(call) == [COUNT_ROWS, A, B]
    assert ir.map_children(call, lambda child: C) == ir.WindowCall(
        C,  # type: ignore[arg-type]
        (C,),
        (ir.SortKey(C, True, False),),
    )


# --- Offset functions and window frames --------------------------------------


@pytest.mark.parametrize(
    ("frame", "method"),
    [
        (ir.WindowFrame(True, -2, 0), ".rowsBetween(-2, Window.currentRow)"),
        (ir.WindowFrame(True, 0, 3), ".rowsBetween(Window.currentRow, 3)"),
        (
            ir.WindowFrame(True, None, None),
            ".rowsBetween(Window.unboundedPreceding, Window.unboundedFollowing)",
        ),
        (
            ir.WindowFrame(False, None, 0),
            ".rangeBetween(Window.unboundedPreceding, Window.currentRow)",
        ),
    ],
)
def test_frames_are_written_as_window_methods(
    frame: ir.WindowFrame, method: str
) -> None:
    call = ir.WindowCall(COUNT_ROWS, (), (ir.SortKey(A, False, True),), frame)

    code = emit(Project(TableScan(("t",)), (Alias(call, "n"),)))

    assert method in code


def test_windows_with_different_frames_are_different_variables() -> None:
    order = (ir.SortKey(A, False, True),)
    plan = Project(
        TableScan(("t",)),
        (
            Alias(ir.WindowCall(COUNT_ROWS, (), order), "running"),
            Alias(
                ir.WindowCall(COUNT_ROWS, (), order, ir.WindowFrame(True, -1, 0)),
                "pair",
            ),
        ),
    )

    code = emit(plan)

    assert 'window_1 = Window.orderBy(F.col("a").asc())\n' in code
    assert (
        'window_2 = Window.orderBy(F.col("a").asc())'
        ".rowsBetween(-1, Window.currentRow)\n"
    ) in code


@pytest.mark.parametrize(
    ("function", "code"),
    [
        (ir.FunctionCall("lag", (A,)), 'F.lag(F.col("a"))'),
        (
            ir.FunctionCall("lead", (A, Literal(2), Literal(0))),
            'F.lead(F.col("a"), 2, 0)',
        ),
        (ir.FunctionCall("ntile", (Literal(4),)), "F.ntile(4)"),
        (
            ir.FunctionCall("first_value", (A, Literal(True))),
            'F.first_value(F.col("a"), ignoreNulls=True)',
        ),
    ],
)
def test_offset_and_value_function_arguments(
    function: ir.FunctionCall, code: str
) -> None:
    call = ir.WindowCall(function, (), (ir.SortKey(B, False, True),))

    assert emit_expression(call).startswith(f"{code}.over(")


@pytest.mark.parametrize(
    ("rows", "start", "end"),
    [(False, -1, 0), (False, 0, 2), (True, 2, 1)],
)
def test_invalid_window_frames_are_rejected(
    rows: bool, start: int | None, end: int | None
) -> None:
    with pytest.raises(ValueError, match=r"RANGE frame|cannot end before"):
        ir.WindowFrame(rows, start, end)


# --- Named relations: CTEs and subqueries ------------------------------------

FILTERED = ir.Filter(
    TableScan(("orders",)), BinaryOp(BinaryOperator.GREATER, A, Literal(1))
)


def test_named_relation_is_declared_once_before_the_result() -> None:
    named = ir.Named("big", FILTERED)
    plan = ir.Join(
        ir.RelationAlias(named, "x"), ir.RelationAlias(named, "y"), ir.JoinKind.CROSS
    )

    assert emit(plan) == (
        "from pyspark.sql import functions as F\n"
        "\n"
        'orders = spark.table("orders")\n'
        "\n"
        "big = (\n"
        "    orders\n"
        '    .where(F.col("a") > F.lit(1))\n'
        ")\n"
        "\n"
        "result = (\n"
        '    big.alias("x")\n'
        '    .crossJoin(big.alias("y"))\n'
        ")\n"
    )


def test_named_relations_are_declared_after_the_ones_they_use() -> None:
    inner = ir.Named("first_step", FILTERED)
    outer = ir.Named("second_step", Project(inner, (A,)))

    code = emit(Project(outer, (A,)))

    assert code.index("first_step = (") < code.index("second_step = (")
    assert code.index("second_step = (") < code.index("result = (")


def test_column_list_renames_with_to_df() -> None:
    plan = ir.RenameColumns(TableScan(("t",)), ("x", "y"))

    assert emit(plan).endswith('    .toDF(\n        "x",\n        "y",\n    )\n)\n')


@pytest.mark.parametrize(
    ("plan", "declaration"),
    [
        # A table variable already uses the name.
        (
            ir.Join(
                ir.RelationAlias(ir.Named("orders", FILTERED), "o"),
                ir.RelationAlias(TableScan(("t",)), "t"),
                ir.JoinKind.CROSS,
            ),
            "orders_2 = (",
        ),
        # The generated code already uses the name.
        (ir.Named("result", FILTERED), "result_df = ("),
        (ir.Named("class", FILTERED), "class_df = ("),
        (ir.Named("2024 sales", FILTERED), "t_2024_sales = ("),
    ],
)
def test_named_relation_names_never_clash(plan: ir.Relation, declaration: str) -> None:
    assert declaration in emit(plan)


def test_different_relations_with_the_same_name_get_different_variables() -> None:
    first = ir.Named("s", FILTERED)
    second = ir.Named("s", TableScan(("t",)))
    third = ir.Named("s", TableScan(("u",)))
    plan = ir.Join(
        ir.Join(
            ir.RelationAlias(first, "a"),
            ir.RelationAlias(second, "b"),
            ir.JoinKind.CROSS,
        ),
        ir.RelationAlias(third, "c"),
        ir.JoinKind.CROSS,
    )

    code = emit(plan)

    assert "s = (\n    orders\n" in code
    assert "s_2 = t\n" in code
    assert "s_3 = u\n" in code


def test_rename_columns_requires_names() -> None:
    with pytest.raises(ValueError, match="at least one name"):
        ir.RenameColumns(TableScan(("t",)), ())


# --- Set operations ----------------------------------------------------------

T_ONLY = TableScan(("t",))
U_FILTERED = ir.Filter(
    TableScan(("u",)), BinaryOp(BinaryOperator.GREATER, A, Literal(1))
)


@pytest.mark.parametrize(
    ("operator", "distinct", "steps"),
    [
        (ir.SetOperator.UNION, False, ".union("),
        (ir.SetOperator.UNION, True, ".union("),
        (ir.SetOperator.INTERSECT, True, ".intersect("),
        (ir.SetOperator.INTERSECT, False, ".intersectAll("),
        (ir.SetOperator.EXCEPT, True, ".subtract("),
        (ir.SetOperator.EXCEPT, False, ".exceptAll("),
    ],
)
def test_set_operation_methods(
    operator: ir.SetOperator, distinct: bool, steps: str
) -> None:
    code = emit(ir.SetOperation(operator, T_ONLY, U_FILTERED, distinct))

    assert f"    {steps}\n" in code
    # Only UNION needs an extra step to remove duplicates.
    removes_duplicates = operator is ir.SetOperator.UNION and distinct
    assert ("    .distinct()\n" in code) is removes_duplicates


def test_other_branch_is_a_nested_chain() -> None:
    plan = ir.SetOperation(ir.SetOperator.UNION, T_ONLY, U_FILTERED, True)

    assert emit(plan) == (
        "from pyspark.sql import functions as F\n"
        "\n"
        "result = (\n"
        '    spark.table("t")\n'
        "    .union(\n"
        '        spark.table("u")\n'
        '        .where(F.col("a") > F.lit(1))\n'
        "    )\n"
        "    .distinct()\n"
        ")\n"
    )


def test_branch_without_steps_stays_on_one_line() -> None:
    plan = ir.SetOperation(ir.SetOperator.UNION, U_FILTERED, T_ONLY, False)

    assert '    .union(spark.table("t"))\n' in emit(plan)


def test_nested_branches_respect_the_line_length() -> None:
    long_names = tuple(Column((f"a_rather_long_column_name_{i}",)) for i in range(3))
    deep = Project(TableScan(("u",)), long_names)
    plan = ir.SetOperation(
        ir.SetOperator.UNION,
        T_ONLY,
        ir.SetOperation(ir.SetOperator.INTERSECT, T_ONLY, deep, True),
        True,
    )

    code = emit(plan)

    assert max(len(line) for line in code.splitlines()) <= 88
    assert '                F.col("a_rather_long_column_name_0"),\n' in code


def test_named_relation_first_used_in_a_branch_is_declared_unindented() -> None:
    named = ir.Named("recent", U_FILTERED)
    plan = ir.SetOperation(ir.SetOperator.UNION, T_ONLY, Project(named, (A,)), True)

    code = emit(plan)

    assert "recent = (\n    spark.table" in code
    assert "    .union(\n        recent\n" in code


# --- Subqueries in expressions -----------------------------------------------

ORDERS_SCAN = TableScan(("orders",))
SUBQUERY = ir.Named("subquery", Project(ORDERS_SCAN, (Column(("customer_id",)),)))


@pytest.mark.parametrize(
    ("expression", "code"),
    [
        (ir.OuterColumn(("c", "customer_id")), 'F.col("c.customer_id").outer()'),
        (ir.InSubquery(A, SUBQUERY), 'F.col("a").isin(subquery)'),
        (
            UnaryOp(UnaryOperator.NOT, ir.InSubquery(A, SUBQUERY)),
            '~F.col("a").isin(subquery)',
        ),
        (ir.Exists(SUBQUERY), "subquery.exists()"),
        (ir.ScalarSubquery(SUBQUERY), "subquery.scalar()"),
    ],
)
def test_subquery_expressions(expression: ir.Expression, code: str) -> None:
    plan = ir.Filter(TableScan(("customers",)), expression)

    assert f"    .where({code})\n" in emit(plan)


def test_subquery_is_declared_before_the_query_that_uses_it() -> None:
    plan = ir.Filter(TableScan(("customers",)), ir.Exists(SUBQUERY))

    assert emit(plan) == (
        "from pyspark.sql import functions as F\n"
        "\n"
        "subquery = (\n"
        '    spark.table("orders")\n'
        '    .select(F.col("customer_id"))\n'
        ")\n"
        "\n"
        "result = (\n"
        '    spark.table("customers")\n'
        "    .where(subquery.exists())\n"
        ")\n"
    )


def test_windows_and_joins_inside_subqueries_are_declared() -> None:
    ranked = ir.WindowCall(ir.WindowFunction.RANK, (), (ir.SortKey(A, False, True),))
    joined = ir.Join(
        ir.RelationAlias(ORDERS_SCAN, "o"),
        ir.RelationAlias(TableScan(("items",)), "i"),
        ir.JoinKind.CROSS,
    )
    inner = ir.Named("subquery", Project(joined, (Alias(ranked, "r"),)))
    plan = ir.Filter(TableScan(("customers",)), ir.Exists(inner))

    code = emit(plan)

    assert 'window = Window.orderBy(F.col("a").asc())\n' in code
    assert 'orders = spark.table("orders")\n' in code
    assert 'items = spark.table("items")\n' in code
