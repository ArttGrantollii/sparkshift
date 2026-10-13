"""Tests for translating SAS programs into the IR."""

import pytest

from sparkshift import ir
from sparkshift.errors import UnsupportedSQLError
from sparkshift.ir import (
    BinaryOp,
    BinaryOperator,
    Column,
    FunctionCall,
    IsNull,
    Literal,
    TableScan,
    UnaryOp,
    UnaryOperator,
)
from sparkshift.sas_parser import parse_sas
from sparkshift.sas_translate import SasTranslation, translate_sas

X = Column(("x",))


def translate(source: str) -> SasTranslation:
    return translate_sas(parse_sas(source), source)


def issues(source: str) -> list[str]:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate(source)
    return [issue.message for issue in caught.value.issues]


def condition(where: str) -> ir.Expression:
    """The IR of a WHERE condition."""
    [dataset] = translate(f"data a; set b; where {where}; run;").datasets
    assert isinstance(dataset.source, ir.Filter)
    return dataset.source.condition


def text(variable: ir.Expression) -> ir.Expression:
    return FunctionCall("coalesce", (FunctionCall("rtrim", (variable,)), Literal("")))


def present_and(condition: ir.Expression) -> ir.Expression:
    return BinaryOp(BinaryOperator.AND, IsNull(X, negated=True), condition)


def missing_or(condition: ir.Expression) -> ir.Expression:
    return BinaryOp(BinaryOperator.OR, IsNull(X), condition)


def negated(condition: ir.Expression) -> ir.Expression:
    return UnaryOp(UnaryOperator.NOT, condition)


# --- Data sets -----------------------------------------------------------------


def test_a_step_reading_a_permanent_data_set() -> None:
    translation = translate("data Work.Out; set Sales.Customers; run;")

    assert translation.datasets == (ir.Named("out", TableScan(("Sales", "Customers"))),)
    assert translation.warnings == ()


@pytest.mark.parametrize("name", ["customers", "work.customers", "WORK.customers"])
def test_a_temporary_data_set_the_program_did_not_create_is_a_table(name: str) -> None:
    [dataset] = translate(f"data out; set {name}; run;").datasets

    assert dataset.source == TableScan(("customers",))


def test_a_later_step_reads_the_data_set_an_earlier_step_created() -> None:
    first, second = translate("data a; set t; run; data b; set WORK.A; run;").datasets

    assert second.source is first


def test_a_data_set_created_twice_is_read_as_its_latest_version() -> None:
    first, second, third = translate(
        "data a; set t; run; data a; set a; run; data c; set a; run;"
    ).datasets

    assert second.source is first
    assert third.source is second


def test_input_options_where_first_with_the_old_names() -> None:
    [dataset] = translate(
        "data out; set t(rename=(a=x) keep=a where=(a > 1)); run;"
    ).datasets

    a = Column(("a",))
    greater = BinaryOp(BinaryOperator.GREATER, a, Literal(1))
    filtered = ir.Filter(
        TableScan(("t",)),
        BinaryOp(BinaryOperator.AND, IsNull(a, negated=True), greater),
    )
    assert dataset.source == ir.RenameByName(ir.Project(filtered, (a,)), (("a", "x"),))


def test_the_order_of_options_and_statements() -> None:
    [dataset] = translate(
        "data out(drop=y rename=(z=zz)); "
        "set t(rename=(a=x) keep=a b drop=b); "
        "rename x = z; keep x y; where x = 2; run;"
    ).datasets

    # Input: KEEP= and DROP= left to right, then RENAME=.
    kept = ir.Project(TableScan(("t",)), (Column(("a",)), Column(("b",))))
    read = ir.RenameByName(ir.DropColumns(kept, ("b",)), (("a", "x"),))
    # Then the WHERE statement, KEEP, and RENAME statements.
    where = ir.Filter(read, ir.NullSafeEqual(X, Literal(2)))
    statements = ir.RenameByName(ir.Project(where, (X, Column(("y",)))), (("x", "z"),))
    # Then the output options.
    output = ir.RenameByName(ir.DropColumns(statements, ("y",)), (("z", "zz"),))
    assert dataset.source == output


def test_keep_drop_and_rename_statements_combine() -> None:
    [dataset] = translate(
        "data a; set t; keep x; drop y; keep Y z x; rename x = p; rename z = q; run;"
    ).datasets

    assert dataset.source == ir.RenameByName(
        ir.DropColumns(
            ir.Project(TableScan(("t",)), (X, Column(("Y",)), Column(("z",)))),
            ("y",),
        ),
        (("x", "p"), ("z", "q")),
    )


def test_keeping_several_variables_warns_about_their_order() -> None:
    warnings = translate("data a; set t(keep=x y); keep x; run;").warnings

    assert [(w.message, w.sql) for w in warnings] == [
        ("KEEP of several variables", "keep=x y")
    ]


def test_a_where_statement_is_ignored_next_to_where_option() -> None:
    translation = translate("data a; set t(where=(x = 1)); where x = 2; run;")

    [dataset] = translation.datasets
    assert dataset.source == ir.Filter(
        TableScan(("t",)), ir.NullSafeEqual(X, Literal(1))
    )
    assert [(w.message, w.sql) for w in translation.warnings] == [
        ("WHERE statement ignored", "where x = 2")
    ]


# --- Conditions ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        # Numbers: a missing value is smaller than every number.
        ("x = 1", ir.NullSafeEqual(X, Literal(1))),
        ("x ^= 1", negated(ir.NullSafeEqual(X, Literal(1)))),
        ("x < 1", missing_or(BinaryOp(BinaryOperator.LESS, X, Literal(1)))),
        ("x <= 1.5", missing_or(BinaryOp(BinaryOperator.LESS_EQUAL, X, Literal(1.5)))),
        ("x > 1", present_and(BinaryOp(BinaryOperator.GREATER, X, Literal(1)))),
        (
            "x >= -2",
            present_and(BinaryOp(BinaryOperator.GREATER_EQUAL, X, Literal(-2))),
        ),
        # The constant may come first.
        ("1 < x", present_and(BinaryOp(BinaryOperator.GREATER, X, Literal(1)))),
        ("1 >= x", missing_or(BinaryOp(BinaryOperator.LESS_EQUAL, X, Literal(1)))),
        # The missing value: nothing is smaller.
        ("x = .", IsNull(X)),
        (". = x", IsNull(X)),
        ("x ^= .", IsNull(X, negated=True)),
        ("x > .", IsNull(X, negated=True)),
        ("x >= .", Literal(True)),
        ("x < .", Literal(False)),
        ("x <= .", IsNull(X)),
        # Text: missing is blank, and trailing blanks do not count.
        ("x = 'US  '", BinaryOp(BinaryOperator.EQUAL, text(X), Literal("US"))),
        ("x < ' '", BinaryOp(BinaryOperator.LESS, text(X), Literal(""))),
        ("'b' > x", BinaryOp(BinaryOperator.LESS, text(X), Literal("b"))),
        # Lists and ranges.
        ("x in (1, 2)", present_and(ir.InList(X, (Literal(1), Literal(2))))),
        ("x in (1 .)", missing_or(ir.InList(X, (Literal(1),)))),
        ("x in (.)", IsNull(X)),
        ("x in ('a', 'b ')", ir.InList(text(X), (Literal("a"), Literal("b")))),
        ("x not in (1)", negated(present_and(ir.InList(X, (Literal(1),))))),
        ("x between 1 and 5", present_and(ir.Between(X, Literal(1), Literal(5)))),
        ("x between 'a' and 'c'", ir.Between(text(X), Literal("a"), Literal("c"))),
        (
            "x not between 1 and 5",
            negated(present_and(ir.Between(X, Literal(1), Literal(5)))),
        ),
        # Missing numbers are NULL; missing text is NULL or blank.
        (
            "x is missing",
            missing_or(
                BinaryOp(BinaryOperator.EQUAL, FunctionCall("rtrim", (X,)), Literal(""))
            ),
        ),
    ],
)
def test_conditions_follow_sas_rules(where: str, expected: ir.Expression) -> None:
    assert condition(where) == expected


def test_logical_operators() -> None:
    one = ir.NullSafeEqual(X, Literal(1))

    assert condition("x = 1 and not (x = 1) or x = 1") == BinaryOp(
        BinaryOperator.OR,
        BinaryOp(BinaryOperator.AND, one, negated(one)),
        one,
    )
    assert condition("x is not null") == negated(condition("x is missing"))


# --- Unsupported constructs ----------------------------------------------------


@pytest.mark.parametrize(
    ("where", "message"),
    [
        ("x = y", "comparison of two variables"),
        ("1 = 2", "comparison of two constants"),
        ("x + 1 = 'a'", "comparison of text with a number"),
        ("'a' + 1 > 2", "arithmetic on text"),
        ("-'a' > 2", "arithmetic on text"),
        ("scan(x) = 'A'", "function SCAN"),
        ("x || 'a' = 'b'", "concatenation with ||"),
        ("x ** 2 > 1", "the ** operator"),
        ("x", "a variable whose type SparkShift cannot see as a condition"),
        ("1", "a constant as a condition"),
        ("'a'", "text as a condition"),
        ("1 in (1)", "IN on a constant"),
        ("x in (y)", "variable in an IN list"),
        ("x in ('a', 1)", "IN list of text and numbers"),
        ("x + 1 in ('a')", "IN list of text and numbers"),
        ("1 between 0 and 2", "BETWEEN other than a value between two constants"),
        ("x between . and 2", "BETWEEN other than a value between two constants"),
        ("x between y and 2", "BETWEEN other than a value between two constants"),
        ("x between 1 and 'b'", "BETWEEN of text and numbers"),
        ("x + 1 between 'a' and 'b'", "BETWEEN of text and numbers"),
        ("1 is missing", "IS MISSING on a constant"),
        ("x = 1 and scan(x) = 'A'", "function SCAN"),
        ("scan(x) = 'A' or x = 1", "function SCAN"),
        ("not (scan(x) = 'A')", "function SCAN"),
        ("x in (scan(y))", "function SCAN"),
        ("scan(x) in (1)", "function SCAN"),
        ("scan(x) is missing", "function SCAN"),
        ("x between scan(a) and 'b'", "function SCAN"),
        ("x = scan(y)", "function SCAN"),
        ("scan(y) + 1 > 0", "function SCAN"),
        ("x * scan(y) > 0", "function SCAN"),
        ("-scan(y) > 0", "function SCAN"),
        ("scan(y)", "function SCAN"),
    ],
)
def test_unsupported_conditions(where: str, message: str) -> None:
    assert issues(f"data a; set b; where {where}; run;") == [message]


@pytest.mark.parametrize(
    ("program", "message"),
    [
        ("data a; keep x; run;", "DATA step without a SET statement"),
        ("data a; set b; set c; run;", "second SET statement"),
        ("data a; set b; where x = 1; where x = 2; run;", "second WHERE statement"),
        ("data a; set b c; run;", "SET with several data sets"),
        ("data a b; set c; run;", "DATA statement with several data sets"),
        ("data; set c; run;", "DATA statement without a data set"),
        ("data _null_; set c; run;", "DATA _NULL_ step"),
        ("data sales.a; set c; run;", "DATA step writing to library SALES"),
        ("data a(where=(x = 1)); set c; run;", "WHERE= option on the output data set"),
        ("data a; set c(where=(x = 1) where=(x = 2)); run;", "second WHERE= option"),
        ("data a; set c(where=(x = y)); run;", "comparison of two variables"),
        ("data a; set c; rename x = y y = x; run;", "renames that swap or chain names"),
        ("data a; set c(rename=(x=y x=z)); run;", "renames that swap or chain names"),
    ],
)
def test_unsupported_steps(program: str, message: str) -> None:
    assert issues(program) == [message]


def test_conditions_of_an_untranslated_step_are_still_checked() -> None:
    assert issues("data a; set b c; where x = y; run;") == [
        "SET with several data sets",
        "comparison of two variables",
    ]
    assert issues("data a; where scan(x) = 'A'; total = y ** 2; run;") == [
        "DATA step without a SET statement",
        "function SCAN",
        "the ** operator",
    ]


def test_every_issue_in_the_program_is_reported() -> None:
    assert issues(
        "proc transpose data=a; by x; run;\n"
        "data b; set a; where x = y; total = scan(z); run;\n"
        "data _null_; set b; run;"
    ) == [
        "PROC TRANSPOSE step",
        "comparison of two variables",
        "function SCAN",
        "DATA _NULL_ step",
    ]


def test_issues_carry_the_source_and_a_hint() -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate("data a; set b;\n  where x =\n    y; run;")

    [issue] = caught.value.issues
    assert issue.sql == "x = y"
    assert issue.hint is not None and "Compare a variable with a constant" in issue.hint


# --- Program statements --------------------------------------------------------

T = TableScan(("t",))
Y = Column(("y",))
S = Column(("s",))
GREATER_1 = present_and(BinaryOp(BinaryOperator.GREATER, X, Literal(1)))


def step(body: str, source: str = "t") -> ir.Relation:
    """The relation of one DATA step reading ``source``."""
    [dataset] = translate(f"data a; set {source}; {body} run;").datasets
    return dataset.source


def warnings(body: str, source: str = "t") -> list[str]:
    return [w.message for w in translate(f"data a; set {source}; {body} run;").warnings]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "y = x * (1 - z);",
            BinaryOp(
                BinaryOperator.MULTIPLY,
                X,
                BinaryOp(BinaryOperator.SUBTRACT, Literal(1), Column(("z",))),
            ),
        ),
        (
            "y = x + 2 - 1;",
            BinaryOp(
                BinaryOperator.SUBTRACT,
                BinaryOp(BinaryOperator.ADD, X, Literal(2)),
                Literal(1),
            ),
        ),
        # Division by zero gives a missing value in SAS.
        ("y = x / 2;", FunctionCall("try_divide", (X, Literal(2)))),
        ("y = .;", Literal(None)),
        ("y = x + .;", BinaryOp(BinaryOperator.ADD, X, Literal(None))),
        ("y = -x;", UnaryOp(UnaryOperator.NEGATE, X)),
        ("y = +x;", X),
        ("y = -2;", Literal(-2)),
        ("y = 'text';", Literal("text")),
        # A comparison is 1 or 0.
        ("y = (x > 1);", ir.Cast(GREATER_1, "int")),
        ("y = not (x > 1);", ir.Cast(negated(GREATER_1), "int")),
        (
            "y = x in (1, 2);",
            ir.Cast(present_and(ir.InList(X, (Literal(1), Literal(2)))), "int"),
        ),
        (
            "y = x between 1 and 2;",
            ir.Cast(present_and(ir.Between(X, Literal(1), Literal(2))), "int"),
        ),
        (
            "y = x is missing;",
            ir.Cast(
                missing_or(
                    BinaryOp(
                        BinaryOperator.EQUAL, FunctionCall("rtrim", (X,)), Literal("")
                    )
                ),
                "int",
            ),
        ),
    ],
)
def test_assignments(body: str, expected: ir.Expression) -> None:
    assert step(body) == ir.WithColumn(T, "y", expected)


def test_assignments_apply_in_order() -> None:
    assert step("y = x + 1; z = y * 2;") == ir.WithColumn(
        ir.WithColumn(T, "y", BinaryOp(BinaryOperator.ADD, X, Literal(1))),
        "z",
        BinaryOp(BinaryOperator.MULTIPLY, Y, Literal(2)),
    )


def test_an_if_else_chain_is_one_case() -> None:
    relation = step(
        "if x > 1 then s = 'large'; else if x > 0 then s = 'small'; else s = 'none';",
        "t(keep=x)",
    )

    greater_0 = present_and(BinaryOp(BinaryOperator.GREATER, X, Literal(0)))
    assert isinstance(relation, ir.WithColumn)
    assert relation.expression == ir.Case(
        ((GREATER_1, Literal("large")), (greater_0, Literal("small"))),
        Literal("none"),
    )


@pytest.mark.parametrize(
    ("body", "source", "default"),
    [
        # A new variable is missing where it is not assigned.
        ("if x > 1 then y = 1;", "t(keep=x)", None),
        ("if x > 1 then do; y = 1; end;", "t(keep=x)", None),
        # An input variable keeps its value: it is read, or listed by KEEP=.
        ("if x > 1 then x = 0;", "t", X),
        ("if x > 1 then y = 1;", "t(keep=x y)", Y),
        ("if x > 1 then y = y + 1;", "t", Y),
        # So does a variable assigned before.
        ("y = 2; if x > 1 then y = 1;", "t", Y),
    ],
)
def test_without_else_a_variable_keeps_its_value_or_is_missing(
    body: str, source: str, default: ir.Expression | None
) -> None:
    relation = step(body, source)

    assert isinstance(relation, ir.WithColumn)
    case = relation.expression
    assert isinstance(case, ir.Case)
    assert case.default == default


def test_input_columns_follow_keep_drop_and_rename() -> None:
    relation = step(
        "if c > 1 then b = 1; if c > 1 then c = 0;",
        "t(keep=a b drop=b rename=(a=c z=w))",
    )

    assert isinstance(relation, ir.WithColumn) and relation.name == "c"
    assert isinstance(relation.expression, ir.Case)
    assert relation.expression.default == Column(("c",))
    assert isinstance(relation.source, ir.WithColumn)
    assert isinstance(relation.source.expression, ir.Case)
    assert relation.source.expression.default is None  # b was dropped: it is new


def test_a_block_assigns_each_variable_under_its_conditions() -> None:
    relation = step("if x > 1 then do; y = 1; s = 'a'; end; else s = 'b';", "t(keep=x)")

    otherwise = negated(GREATER_1)
    assert relation == ir.WithColumn(
        ir.WithColumn(
            ir.WithColumn(
                ir.Project(T, (X,)), "y", ir.Case(((GREATER_1, Literal(1)),))
            ),
            "s",
            ir.Case(((GREATER_1, Literal("a")),)),
        ),
        "s",
        ir.Case(((otherwise, Literal("b")),), S),
    )


def test_a_condition_the_branch_changes_is_kept_in_a_helper_column() -> None:
    relation = step("if x > 1 then do; x = 0; y = 1; end;", "t(keep=x)")

    name = ir.HelperName("_if_1")
    helper = ir.HelperColumn(name)
    checked_source = ir.AllocateHelpers(ir.Project(T, (X,)), (name,), ("x", "y"))
    assert relation == ir.DropColumns(
        ir.WithColumn(
            ir.WithColumn(
                ir.WithColumn(checked_source, name, GREATER_1),
                "x",
                ir.Case(((helper, Literal(0)),), X),
            ),
            "y",
            ir.Case(((helper, Literal(1)),)),
        ),
        (name,),
    )


def test_nested_conditions_combine() -> None:
    relation = step(
        "if y = 1 then do; if x > 1 then s = 'a'; else s = 'b'; end;", "t(keep=x y)"
    )

    outer = ir.NullSafeEqual(Y, Literal(1))
    assert isinstance(relation, ir.WithColumn)
    assert relation.expression == ir.Case(
        (
            (BinaryOp(BinaryOperator.AND, outer, GREATER_1), Literal("a")),
            (outer, Literal("b")),
        )
    )


def test_an_else_branch_with_a_block() -> None:
    relation = step("if x > 1 then s = 'a'; else do; s = 'b'; y = 2; end;", "t(keep=x)")

    otherwise = negated(GREATER_1)
    assert relation == ir.WithColumn(
        ir.WithColumn(
            ir.WithColumn(
                ir.Project(T, (X,)), "s", ir.Case(((GREATER_1, Literal("a")),))
            ),
            "s",
            ir.Case(((otherwise, Literal("b")),), S),
        ),
        "y",
        ir.Case(((otherwise, Literal(2)),)),
    )


@pytest.mark.parametrize(
    ("body", "kept"),
    [
        ("if x > 1;", GREATER_1),
        ("if x > 1 then delete;", negated(GREATER_1)),
        ("delete;", Literal(False)),
        (
            "if y = 1 then do; if x > 1; end;",
            negated(
                BinaryOp(
                    BinaryOperator.AND,
                    ir.NullSafeEqual(Y, Literal(1)),
                    negated(GREATER_1),
                )
            ),
        ),
    ],
)
def test_rows_are_deleted_where_the_program_says(
    body: str, kept: ir.Expression
) -> None:
    assert step(body) == ir.Filter(T, kept)


def test_a_number_alone_is_true_unless_zero_or_missing() -> None:
    relation = step("big = (x > 1); if big;")

    big = Column(("big",))
    assert relation == ir.Filter(
        ir.WithColumn(T, "big", ir.Cast(GREATER_1, "int")),
        BinaryOp(
            BinaryOperator.AND,
            IsNull(big, negated=True),
            BinaryOp(BinaryOperator.NOT_EQUAL, big, Literal(0)),
        ),
    )


@pytest.mark.parametrize(
    ("where", "expected"),
    [
        # Both sides may be missing, the smallest value.
        (
            "x + 1 = y",
            ir.NullSafeEqual(BinaryOp(BinaryOperator.ADD, X, Literal(1)), Y),
        ),
        (
            "x < y + 1",
            FunctionCall(
                "coalesce",
                (
                    BinaryOp(
                        BinaryOperator.LESS,
                        X,
                        BinaryOp(BinaryOperator.ADD, Y, Literal(1)),
                    ),
                    BinaryOp(
                        BinaryOperator.AND,
                        IsNull(X),
                        IsNull(
                            BinaryOp(BinaryOperator.ADD, Y, Literal(1)), negated=True
                        ),
                    ),
                ),
            ),
        ),
        (
            "x * 2 >= y",
            FunctionCall(
                "coalesce",
                (
                    BinaryOp(
                        BinaryOperator.LESS_EQUAL,
                        Y,
                        BinaryOp(BinaryOperator.MULTIPLY, X, Literal(2)),
                    ),
                    IsNull(Y),
                ),
            ),
        ),
        (
            "x + 1 ^= y",
            negated(ir.NullSafeEqual(BinaryOp(BinaryOperator.ADD, X, Literal(1)), Y)),
        ),
        ("x / 2 = .", IsNull(FunctionCall("try_divide", (X, Literal(2))))),
        ("x + 1 is missing", IsNull(BinaryOp(BinaryOperator.ADD, X, Literal(1)))),
    ],
)
def test_comparisons_of_two_values(where: str, expected: ir.Expression) -> None:
    assert condition(where) == expected


def test_a_text_variable_the_step_assigns_compares_as_text() -> None:
    relation = step("s = 'a'; if s = y;")

    assert relation == ir.Filter(
        ir.WithColumn(T, "s", Literal("a")),
        BinaryOp(BinaryOperator.EQUAL, text(S), text(Y)),
    )


# --- Text lengths --------------------------------------------------------------


def test_a_new_text_variable_truncates_longer_values() -> None:
    body = "if x > 1 then grade = 'High'; else grade = 'Medium';"

    relation = step(body, "t(keep=x)")

    assert isinstance(relation, ir.WithColumn)
    assert relation.expression == ir.Case(
        ((GREATER_1, Literal("High")),), Literal("Medi")
    )
    assert (
        warnings(body, "t(keep=x)")
        == [
            "KEEP of several variables",
            "text truncated to the length of grade",
        ][1:]
    )


def test_a_length_statement_sets_the_length() -> None:
    relation = step(
        "length grade $ 6 code $ 2; "
        "if x > 1 then grade = 'High'; else grade = 'Medium'; "
        "code = y;"
    )

    assert isinstance(relation, ir.WithColumn) and relation.name == "code"
    assert relation.expression == FunctionCall("substring", (Y, Literal(1), Literal(2)))
    assert isinstance(relation.source, ir.WithColumn)
    assert relation.source.expression == ir.Case(
        ((GREATER_1, Literal("High")),), Literal("Medium")
    )


def test_a_length_after_the_first_use_is_ignored() -> None:
    assert (
        warnings("s = 'a'; length s $ 5; length x 8;")
        == [
            "LENGTH of s ignored",
            "LENGTH of x ignored",
        ][:1]
    )


def test_a_numeric_length_declares_a_number() -> None:
    assert step("length n 8; n = x;") == ir.WithColumn(T, "n", X)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "if x > 1 then grade = 'High'; else grade = 'Medium';",
            "text assigned to grade that SAS may truncate",
        ),
        ("s = y; s = 'abc';", "text assigned to s that SAS may truncate"),
        ("s = 'ab'; s = y;", "text assigned to s that SAS may truncate"),
        ("s = 'a'; c = s; c = 'longer';", "text assigned to c that SAS may truncate"),
        (
            "if x > 1 then flag = 1;",
            "conditional assignment of flag, which may be new or an input variable",
        ),
        ("s = 'a'; s = 1;", "assignment of a number to the text variable s"),
        ("n = 1; n = 'a';", "assignment of text to the number variable n"),
        ("length n 4;", "numeric LENGTH 4"),
        ("y = x ** 2;", "the ** operator"),
        ("y = x || 'a';", "concatenation with ||"),
        ("y = scan(x);", "function SCAN"),
        ("y = 'a' * 2;", "arithmetic on text"),
        ("y = x = z;", "comparison of two variables"),
        ("if y = 1 then x = scan(z); else x = 1;", "function SCAN"),
        ("if scan(z) = 'A' then x = 1; else x = 2;", "function SCAN"),
        ("if scan(z) = 'A' then do; x = 1; end; else x = 2;", "function SCAN"),
        (
            "if x then y = 1; else y = 2;",
            "a variable whose type SparkShift cannot see as a condition",
        ),
        ("if x > 1;  if scan(z);", "function SCAN"),
    ],
)
def test_unsupported_program_statements(body: str, message: str) -> None:
    assert issues(f"data a; set t; {body} run;") == [message]


def test_a_length_statement_before_set_is_reported() -> None:
    assert issues("data a; length s $ 5; set t; run;") == [
        "LENGTH statement before SET"
    ]


def test_a_new_variable_known_from_keep_gets_its_first_values_length() -> None:
    # KEEP= shows grade is new: its length is that of 'High'.
    assert warnings("grade = 'High'; grade = 'Medium';", "t(keep=x)") == [
        "text truncated to the length of grade"
    ]
    # An input variable keeps the length it has, which SparkShift cannot see.
    assert warnings("grade = 'High'; grade = 'Medium';", "t(keep=grade x)") == [
        "KEEP of several variables"
    ]


def test_statements_in_blocks_count_for_keep_drop_and_rename() -> None:
    relation = step("if x > 1 then do; y = 1; keep y; end;", "t(keep=x)")

    assert isinstance(relation, ir.Project)
    assert relation.items == (Y,)


# --- Functions -----------------------------------------------------------------

A = Column(("a",))
B = Column(("b",))


def value_of(expression: str) -> ir.Expression:
    """The IR of an expression assigned to y."""
    relation = step(f"y = {expression};")
    assert isinstance(relation, ir.WithColumn)
    return relation.expression


def coalesced(variable: ir.Expression) -> ir.Expression:
    return FunctionCall("coalesce", (variable, Literal(0)))


def is_present(variable: ir.Expression) -> ir.Expression:
    return ir.Cast(IsNull(variable, negated=True), "int")


def plus(left: ir.Expression, right: ir.Expression) -> ir.Expression:
    return BinaryOp(BinaryOperator.ADD, left, right)


def blank_is_null(variable: ir.Expression, trim: str = "trim") -> ir.Expression:
    return FunctionCall("nullif", (FunctionCall(trim, (variable,)), Literal("")))


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        # SUM is missing only when every argument is.
        (
            "sum(a, b)",
            ir.Case(
                (
                    (
                        IsNull(FunctionCall("coalesce", (A, B)), negated=True),
                        plus(coalesced(A), coalesced(B)),
                    ),
                ),
                None,
            ),
        ),
        # With a constant, the sum is never missing.
        ("sum(a, 1)", plus(coalesced(A), Literal(1))),
        ("sum(a, .)", A),
        ("sum(a)", A),
        ("sum(.)", Literal(None)),
        (
            "mean(a, b)",
            FunctionCall(
                "try_divide",
                (plus(coalesced(A), coalesced(B)), plus(is_present(A), is_present(B))),
            ),
        ),
        (
            "mean(a, 4)",
            FunctionCall(
                "try_divide",
                (plus(coalesced(A), Literal(4)), plus(is_present(A), Literal(1))),
            ),
        ),
        ("min(a, b, 0)", FunctionCall("least", (A, B, Literal(0)))),
        ("max(a, .)", A),
        ("coalesce(a, b, 0)", FunctionCall("coalesce", (A, B, Literal(0)))),
        ("n(a, b)", plus(is_present(A), is_present(B))),
        ("n(a, 1, .)", plus(is_present(A), Literal(1))),
        ("n(.)", Literal(0)),
        ("nmiss(a, ., 2)", plus(ir.Cast(IsNull(A), "int"), Literal(1))),
    ],
)
def test_statistics_skip_missing_values(
    expression: str, expected: ir.Expression
) -> None:
    assert value_of(expression) == expected


CEIL_A = FunctionCall("ceil", (BinaryOp(BinaryOperator.SUBTRACT, A, Literal(1e-12)),))
FLOOR_A = FunctionCall("floor", (BinaryOp(BinaryOperator.ADD, A, Literal(1e-12)),))


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("round(a)", FunctionCall("round", (A,))),
        ("round(a, 1)", FunctionCall("round", (A,))),
        ("round(a, 0.01)", FunctionCall("round", (A, Literal(2)))),
        ("round(a, .001)", FunctionCall("round", (A, Literal(3)))),
        ("round(a, 100)", FunctionCall("round", (A, Literal(-2)))),
        ("round(a, 1e-15)", FunctionCall("round", (A, Literal(15)))),
        # Within 1E-12 of an integer counts as that integer.
        ("ceil(a)", CEIL_A),
        ("floor(a)", FLOOR_A),
        (
            "int(a)",
            ir.Case(((BinaryOp(BinaryOperator.LESS, A, Literal(0)), CEIL_A),), FLOOR_A),
        ),
        ("abs(a)", FunctionCall("abs", (A,))),
        ("mod(a, 3)", BinaryOp(BinaryOperator.MODULO, A, Literal(3))),
        ("mod(a, -2)", BinaryOp(BinaryOperator.MODULO, A, Literal(-2))),
        ("round(.)", Literal(None)),
        ("mod(a, .)", Literal(None)),
    ],
)
def test_rounding_functions(expression: str, expected: ir.Expression) -> None:
    assert value_of(expression) == expected


def test_mod_by_a_fraction_is_a_review_item() -> None:
    assert warnings("y = mod(a, 0.5);") == [
        "MOD by a divisor that is not a whole number"
    ]
    assert warnings("y = mod(a, 2);") == []


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("upcase(a)", FunctionCall("upper", (A,))),
        ("lowcase(a)", FunctionCall("lower", (A,))),
        ("strip(a)", FunctionCall("trim", (A,))),
        ("trim(a)", FunctionCall("rtrim", (A,))),
        ("substr(a, 2, 3)", FunctionCall("substring", (A, Literal(2), Literal(3)))),
        ("substr(a, 2)", FunctionCall("substr", (A, Literal(2)))),
        # The last character that is not blank; 1 for blank text.
        (
            "length(a)",
            FunctionCall(
                "greatest",
                (FunctionCall("length", (FunctionCall("rtrim", (A,)),)), Literal(1)),
            ),
        ),
        (
            "cats(a, ' x ', ' ', b)",
            FunctionCall(
                "concat_ws",
                (
                    Literal(""),
                    FunctionCall("trim", (A,)),
                    Literal("x"),
                    FunctionCall("trim", (B,)),
                ),
            ),
        ),
        (
            "catx(', ', a, b)",
            FunctionCall(
                "concat_ws", (Literal(", "), blank_is_null(A), blank_is_null(B))
            ),
        ),
        ("cats(' ')", Literal("")),
        (
            "coalescec(a, b)",
            FunctionCall(
                "coalesce",
                (blank_is_null(A, "rtrim"), blank_is_null(B, "rtrim"), Literal("")),
            ),
        ),
        # A text constant that is not blank ends the search.
        (
            "coalescec(a, ' ', 'none ')",
            FunctionCall("coalesce", (blank_is_null(A, "rtrim"), Literal("none"))),
        ),
        ("coalescec(' ')", Literal("")),
        (
            "missing(a)",
            ir.Cast(
                BinaryOp(
                    BinaryOperator.OR,
                    IsNull(A),
                    BinaryOp(
                        BinaryOperator.EQUAL, FunctionCall("rtrim", (A,)), Literal("")
                    ),
                ),
                "int",
            ),
        ),
        ("missing(a + 1)", ir.Cast(IsNull(plus(A, Literal(1))), "int")),
    ],
)
def test_text_functions(expression: str, expected: ir.Expression) -> None:
    assert value_of(expression) == expected


def test_function_names_ignore_case() -> None:
    assert value_of("UpCase(a)") == FunctionCall("upper", (A,))


def test_functions_are_typed_in_conditions() -> None:
    upper = FunctionCall("upper", (A,))
    # Two text values can be compared: the functions show their type.
    assert condition("upcase(a) = lowcase(b)") == BinaryOp(
        BinaryOperator.EQUAL, text(upper), text(FunctionCall("lower", (B,)))
    )
    assert condition("missing(a)") == BinaryOp(
        BinaryOperator.OR,
        IsNull(A),
        BinaryOp(BinaryOperator.EQUAL, FunctionCall("rtrim", (A,)), Literal("")),
    )
    total = value_of("sum(a, 1)")
    assert condition("sum(a, 1)") == BinaryOp(
        BinaryOperator.AND,
        IsNull(total, negated=True),
        BinaryOp(BinaryOperator.NOT_EQUAL, total, Literal(0)),
    )


def test_a_function_keeps_its_arguments_length() -> None:
    # UPCASE gives code the length of name, 2 here, so 'abc' is truncated.
    body = "name = 'ab'; code = upcase(name); code = 'abc';"
    assert warnings(body, "t(keep=x)") == ["text truncated to the length of code"]


def test_a_value_that_fits_needs_no_substring() -> None:
    # Exactly as long as the variable: nothing is cut.
    relation = step("length s $ 3; s = upcase('abc');")

    assert relation == ir.WithColumn(T, "s", FunctionCall("upper", (Literal("abc"),)))


def test_cats_gives_a_new_variable_length_200() -> None:
    body = "full = cats(a, b); full = 'x';"
    relation = step(body, "t(keep=a b)")

    assert isinstance(relation, ir.WithColumn)
    assert relation.expression == Literal("x")
    assert warnings(body, "t(keep=a b)") == ["KEEP of several variables"]


def test_a_cats_result_longer_than_the_variable_is_blank() -> None:
    relation = step("length full $ 5; full = cats(a, b);")

    assert isinstance(relation, ir.WithColumn)
    cats = FunctionCall(
        "concat_ws",
        (Literal(""), FunctionCall("trim", (A,)), FunctionCall("trim", (B,))),
    )
    too_long = BinaryOp(
        BinaryOperator.GREATER, FunctionCall("length", (cats,)), Literal(5)
    )
    assert relation.expression == ir.Case(((too_long, Literal("")),), cats)


def test_a_substr_result_longer_than_the_variable_is_truncated() -> None:
    relation = step("length code $ 2; code = substr(a, 2);")

    assert isinstance(relation, ir.WithColumn)
    assert relation.expression == FunctionCall(
        "substring", (FunctionCall("substr", (A, Literal(2))), Literal(1), Literal(2))
    )


def test_a_coalescec_result_has_an_unknown_length() -> None:
    assert issues("data a; set t(keep=a b); s = coalescec(a, b); s = 'x'; run;") == [
        "text assigned to s that SAS may truncate"
    ]


@pytest.mark.parametrize(
    ("expression", "message"),
    [
        ("scan(a, 1)", "function SCAN"),
        ("today()", "function TODAY"),
        ("sum()", "SUM with 0 arguments"),
        ("abs(a, b)", "ABS with 2 arguments"),
        ("substr(a)", "SUBSTR with 1 argument"),
        ("catx(',')", "CATX with 1 argument"),
        ("sum(a, 'x')", "text in SUM"),
        ("coalesce(a, 'x')", "text in COALESCE"),
        ("upcase(1)", "number in UPCASE"),
        ("upcase(a + 1)", "number in UPCASE"),
        ("cats(a, 1)", "number in CATS"),
        ("length(.)", "number in LENGTH"),
        ("round(a, 0.5)", "ROUND to a unit that is not a power of 10"),
        ("round(a, b)", "ROUND to a unit that is not a power of 10"),
        ("round(a, -1)", "ROUND to a unit that is not a power of 10"),
        ("round(a, 1e-16)", "ROUND to a unit that is not a power of 10"),
        ("mod(a, 0)", "MOD by a divisor that is not a constant other than 0"),
        ("mod(a, b)", "MOD by a divisor that is not a constant other than 0"),
        (
            "substr(a, b, 2)",
            "SUBSTR with a position or length that is not a positive whole number",
        ),
        (
            "substr(a, 0)",
            "SUBSTR with a position or length that is not a positive whole number",
        ),
        (
            "substr(a, 1, 2.5)",
            "SUBSTR with a position or length that is not a positive whole number",
        ),
        ("catx(d, a, b)", "CATX with a delimiter that is not a text constant"),
        ("missing(1)", "MISSING on a constant"),
        ("sum(of a b)", "OF variable list"),
        ("upcase(scan(a, 1))", "function SCAN"),
        ("sum(scan(a, 1), 1)", "function SCAN"),
        ("round(scan(a, 1))", "function SCAN"),
        ("catx(' ', scan(a, 1))", "function SCAN"),
        ("missing(scan(a, 1))", "function SCAN"),
    ],
)
def test_unsupported_function_calls(expression: str, message: str) -> None:
    assert issues(f"data a; set t; y = {expression}; run;") == [message]


def test_every_text_argument_is_reported() -> None:
    assert issues("data a; set t; y = sum('a', 'b'); run;") == ["text in SUM"] * 2


def test_a_date_function_says_dates_come_later() -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate("data a; set t; y = year(d); run;")

    [issue] = caught.value.issues
    assert issue.hint == "Date functions are not supported yet."


# --- PROC SORT -----------------------------------------------------------------


def ascending(name: str) -> ir.SortKey:
    # Missing values are the smallest: first when ascending.
    return ir.SortKey(Column((name,)), descending=False, nulls_first=True)


def descending(name: str) -> ir.SortKey:
    return ir.SortKey(Column((name,)), descending=True, nulls_first=False)


def test_proc_sort_orders_with_missing_values_first() -> None:
    translation = translate(
        "proc sort data=customers out=sorted; by country descending score; run;"
    )

    assert translation.datasets == (
        ir.Named(
            "sorted",
            ir.Sort(
                TableScan(("customers",)),
                (ascending("country"), descending("score")),
            ),
        ),
    )
    assert translation.warnings == ()


def test_proc_sort_without_out_replaces_the_data_set() -> None:
    first, second = translate(
        "data a; set t; run; proc sort data=a; by x; run;"
    ).datasets

    assert second == ir.Named("a", ir.Sort(first, (ascending("x"),)))


@pytest.mark.parametrize("data", ["", " data=_last_", " data=_LAST_(keep=x y)"])
def test_proc_sort_without_data_sorts_the_last_data_set(data: str) -> None:
    _first, second, third = translate(
        f"data a; set t; run; data b; set a; run; proc sort{data}; by x; run;"
    ).datasets

    assert third.name == "b"
    assert isinstance(third.source, ir.Sort)
    sorted_source = third.source.source
    assert sorted_source is second or (
        isinstance(sorted_source, ir.Project) and sorted_source.source is second
    )


def test_proc_sort_where_filters_before_sorting() -> None:
    [dataset] = translate("proc sort data=t out=s; where x > 1; by y; run;").datasets

    assert dataset.source == ir.Sort(ir.Filter(T, GREATER_1), (ascending("y"),))


def test_proc_sort_applies_data_and_out_options_in_order() -> None:
    [dataset] = translate(
        "proc sort data=t(rename=(a=x)) out=s(drop=y rename=(x=z)); by x; run;"
    ).datasets

    renamed = ir.RenameByName(T, (("a", "x"),))
    sorted_ = ir.Sort(renamed, (ascending("x"),))
    assert dataset.source == ir.RenameByName(
        ir.DropColumns(sorted_, ("y",)), (("x", "z"),)
    )


def test_nodupkey_drops_duplicate_by_values_before_sorting() -> None:
    translation = translate("proc sort data=t out=s nodupkey; by a descending b; run;")

    [dataset] = translation.datasets
    assert dataset.source == ir.Sort(
        ir.DropDuplicates(T, ("a", "b")), (ascending("a"), descending("b"))
    )
    assert [w.message for w in translation.warnings] == [
        "NODUPKEY keeps any row of each BY group"
    ]


def test_nodupkey_keeping_only_by_variables_is_exact() -> None:
    translation = translate("proc sort data=t(keep=a b) out=s nodupkey; by a b a; run;")

    [dataset] = translation.datasets
    assert isinstance(dataset.source, ir.Sort)
    assert dataset.source.source == ir.DropDuplicates(ir.Project(T, (A, B)), ("a", "b"))
    # A variable listed again sorts nothing more.
    assert dataset.source.keys == (ascending("a"), ascending("b"))
    # Only the KEEP= of two variables is a review item.
    assert [w.message for w in translation.warnings] == ["KEEP of several variables"]


@pytest.mark.parametrize(
    ("program", "message"),
    [
        ("proc sort; by x; run;", "PROC SORT without DATA="),
        (
            "proc sort data=t out=perm.s; by x; run;",
            "PROC SORT writing to library PERM",
        ),
        ("proc sort data=perm.t; by x; run;", "PROC SORT replacing PERM.T"),
        (
            "proc sort data=t(where=(x > 1)) out=s; where x < 5; by x; run;",
            "WHERE statement and WHERE= in PROC SORT",
        ),
        (
            "proc sort data=t out=s; where x > 1; where x < 5; by x; run;",
            "second WHERE statement",
        ),
        ("proc sort data=t out=s; where scan(x) = 'a'; by x; run;", "function SCAN"),
        (
            "proc sort data=t out=s(where=(x > 1)); by x; run;",
            "WHERE= option on the output data set",
        ),
    ],
)
def test_unsupported_proc_sorts(program: str, message: str) -> None:
    assert issues(program) == [message]


def test_proc_sort_into_work_and_from_a_permanent_library() -> None:
    [dataset] = translate("proc sort data=perm.t out=work.s; by x; run;").datasets

    assert dataset == ir.Named(
        "s", ir.Sort(TableScan(("perm", "t")), (ascending("x"),))
    )


def test_a_data_step_reads_the_sorted_data_set() -> None:
    sorted_, step = translate(
        "proc sort data=t out=s; by x; run; data b; set s; run;"
    ).datasets

    assert step.source is sorted_


# --- PROC MEANS and SUMMARY ----------------------------------------------------

C = Column(("c",))
FREQ = ir.Alias(ir.AggregateCall(ir.AggregateFunction.COUNT), "_FREQ_")


def type_of(value: int) -> ir.Alias:
    return ir.Alias(Literal(value), "_TYPE_")


def aggregate(function: ir.AggregateFunction, variable: ir.Expression) -> ir.Expression:
    return ir.AggregateCall(function, (variable,))


def present(variable: ir.Expression) -> ir.Expression:
    """Not missing, as a number or as text."""
    return negated(
        BinaryOp(
            BinaryOperator.OR,
            IsNull(variable),
            BinaryOp(
                BinaryOperator.EQUAL, FunctionCall("rtrim", (variable,)), Literal("")
            ),
        )
    )


def means(program: str) -> ir.Relation:
    [dataset] = translate(program).datasets
    return dataset.source


def test_nway_groups_by_the_class_variables_without_missing_values() -> None:
    relation = means(
        "proc means data=t nway; class a; var x; output out=s mean=avg; run;"
    )

    summary = ir.Aggregate(
        ir.Filter(T, present(A)),
        (A,),
        (type_of(1), FREQ, ir.Alias(aggregate(ir.AggregateFunction.AVG, X), "avg")),
    )
    assert relation == ir.Sort(summary, (ascending("a"),))


def test_missing_keeps_missing_class_values() -> None:
    for program in (
        "proc means data=t nway missing; class a b; var x; output out=s n=n; run;",
        "proc means data=t nway; class a b / missing; var x; output out=s n=n; run;",
    ):
        relation = means(program)
        assert isinstance(relation, ir.Sort)
        assert isinstance(relation.source, ir.Aggregate)
        assert relation.source.source == T


def test_missing_on_one_class_statement_applies_to_its_variables() -> None:
    relation = means(
        "proc means data=t nway; class a / missing; class b; var x; "
        "output out=s n=n; run;"
    )

    assert isinstance(relation, ir.Sort)
    assert isinstance(relation.source, ir.Aggregate)
    assert relation.source.source == ir.Filter(T, present(B))


def test_without_nway_every_combination_with_its_type() -> None:
    relation = means(
        "proc summary data=t missing; class a b; var x; output out=s sum=total; run;"
    )

    grouping = ir.AggregateCall(ir.AggregateFunction.GROUPING_ID)
    type_value = BinaryOp(BinaryOperator.SUBTRACT, Literal(3), grouping)
    summary = ir.Aggregate(
        T,
        (A, B),
        (
            ir.Alias(type_value, "_TYPE_"),
            FREQ,
            ir.Alias(aggregate(ir.AggregateFunction.SUM, X), "total"),
        ),
        cube=True,
    )
    order = (ascending("_TYPE_"), ascending("a"), ascending("b"))
    assert relation == ir.Sort(summary, order)


def test_by_groups_keep_missing_values_and_their_order() -> None:
    relation = means(
        "proc means data=t; by descending k; var x; output out=s max=top; run;"
    )

    k = Column(("k",))
    summary = ir.Aggregate(
        T,
        (k,),
        (type_of(0), FREQ, ir.Alias(aggregate(ir.AggregateFunction.MAX, X), "top")),
    )
    assert relation == ir.Sort(summary, (descending("k"),))


def test_the_whole_data_set_is_one_row_with_type_0() -> None:
    relation = means("proc means data=t; var x; output out=s n=count; run;")

    assert relation == ir.Aggregate(
        T,
        (),
        (type_of(0), FREQ, ir.Alias(aggregate(ir.AggregateFunction.COUNT, X), "count")),
    )


@pytest.mark.parametrize(
    ("statistic", "expected"),
    [
        ("n", aggregate(ir.AggregateFunction.COUNT, X)),
        ("nmiss", aggregate(ir.AggregateFunction.COUNT_IF, IsNull(X))),
        ("sum", aggregate(ir.AggregateFunction.SUM, X)),
        ("mean", aggregate(ir.AggregateFunction.AVG, X)),
        ("min", aggregate(ir.AggregateFunction.MIN, X)),
        ("max", aggregate(ir.AggregateFunction.MAX, X)),
        (
            "range",
            BinaryOp(
                BinaryOperator.SUBTRACT,
                aggregate(ir.AggregateFunction.MAX, X),
                aggregate(ir.AggregateFunction.MIN, X),
            ),
        ),
        ("std", aggregate(ir.AggregateFunction.STDDEV_SAMP, X)),
        ("stddev", aggregate(ir.AggregateFunction.STDDEV_SAMP, X)),
        ("var", aggregate(ir.AggregateFunction.VAR_SAMP, X)),
    ],
)
def test_statistics(statistic: str, expected: ir.Expression) -> None:
    relation = means(f"proc means data=t; var x; output out=s {statistic}=v; run;")

    assert isinstance(relation, ir.Aggregate)
    assert relation.aggregates[2] == ir.Alias(expected, "v")


def test_statistic_names_follow_the_variables() -> None:
    relation = means(
        "proc means data=t; var x y; "
        "output out=s mean= sum(y)=total min(y x)=low_y low_x; run;"
    )

    assert isinstance(relation, ir.Aggregate)
    assert [a.name for a in relation.aggregates if isinstance(a, ir.Alias)] == [
        "_TYPE_",
        "_FREQ_",
        "x",
        "y",
        "total",
        "low_y",
        "low_x",
    ]


def test_each_output_statement_creates_a_data_set() -> None:
    first, second = translate(
        "proc means data=t nway; class a; var x; "
        "output out=sums(drop=_type_) sum=total; output out=means mean=average; run;"
    ).datasets

    assert (first.name, second.name) == ("sums", "means")
    assert isinstance(first.source, ir.DropColumns)


def test_proc_means_reads_the_last_data_set_and_a_where_statement() -> None:
    first, second = translate(
        "data a; set t; run; proc means nway; where x > 1; class c; var x; "
        "output out=s n=n; run;"
    ).datasets

    assert isinstance(second.source, ir.Sort)
    assert isinstance(second.source.source, ir.Aggregate)
    assert second.source.source.source == ir.Filter(
        ir.Filter(first, GREATER_1), present(C)
    )


@pytest.mark.parametrize(
    ("program", "message"),
    [
        ("proc means data=t; var x; run;", "PROC MEANS without an OUTPUT statement"),
        ("proc summary; output out=s n=n; run;", "PROC SUMMARY without DATA="),
        ("proc means data=t; var x; output n=n; run;", "OUTPUT without OUT="),
        ("proc means data=t; var x; output out=s; run;", "OUTPUT without statistics"),
        (
            "proc means data=t; var x; output out=perm.s n=n; run;",
            "PROC MEANS writing to library PERM",
        ),
        ("proc means data=t; var x; output out=s median=m; run;", "statistic MEDIAN"),
        (
            "proc means data=t; output out=s mean=m; run;",
            "MEAN without analysis variables",
        ),
        (
            "proc means data=t; var x y; output out=s mean=m; run;",
            "MEAN with 1 name for 2 variables",
        ),
        (
            "proc means data=t; var x; output out=s mean(x y)=m n=; run;",
            "MEAN with 1 name for 2 variables",
        ),
        (
            "proc means data=t; var x; output out=s mean=a sum=a; run;",
            "output variable a named twice",
        ),
        (
            "proc means data=t nway; class a; var x; output out=s mean=a; run;",
            "output variable a named twice",
        ),
        (
            "proc means data=t; by k; class a; var x; output out=s n=n; run;",
            "PROC MEANS with BY and CLASS but without NWAY",
        ),
        (
            "proc means data=t(where=(x > 1)); where x < 5; var x; "
            "output out=s n=n; run;",
            "WHERE statement and WHERE= in PROC MEANS",
        ),
    ],
)
def test_unsupported_proc_means(program: str, message: str) -> None:
    assert issues(program) == [message]


# --- PROC FREQ -----------------------------------------------------------------

COUNT = Column(("COUNT",))


def percentage(column: str, *partition: ir.Expression) -> ir.Expression:
    count = Column((column,))
    total = ir.WindowCall(
        ir.AggregateCall(ir.AggregateFunction.SUM, (count,)), tuple(partition)
    )
    return BinaryOp(
        BinaryOperator.DIVIDE,
        BinaryOp(BinaryOperator.MULTIPLY, count, Literal(100)),
        total,
    )


def counted(
    source: ir.Relation, complete: ir.Expression, *keys: ir.Expression
) -> ir.Relation:
    """The counts, and the helper column of those without missing values."""
    count = ir.Alias(ir.AggregateCall(ir.AggregateFunction.COUNT), "COUNT")
    counts = ir.Aggregate(source, keys, (count,))
    return ir.WithColumn(counts, "_counted", ir.Case(((complete, COUNT),), None))


def test_a_one_way_table() -> None:
    [dataset] = translate(
        "proc freq data=t noprint; tables a / out=counts; run;"
    ).datasets

    percent = ir.WithColumn(
        counted(T, present(A), A), "PERCENT", percentage("_counted")
    )
    assert dataset == ir.Named(
        "counts",
        ir.Sort(ir.DropColumns(percent, ("_counted",)), (ascending("a"),)),
    )


def test_missing_values_count_in_percentages_with_missing() -> None:
    [dataset] = translate(
        "proc freq data=t; tables a / out=counts missing; run;"
    ).datasets

    count = ir.Alias(ir.AggregateCall(ir.AggregateFunction.COUNT), "COUNT")
    assert isinstance(dataset.source, ir.Sort)
    assert dataset.source.source == ir.WithColumn(
        ir.Aggregate(T, (A,), (count,)), "PERCENT", percentage("COUNT")
    )


def test_a_two_way_table_with_row_and_column_percentages() -> None:
    [dataset] = translate(
        "proc freq data=t; by k; tables a b*c / out=cells outpct; run;"
    ).datasets

    k = Column(("k",))
    complete = BinaryOp(BinaryOperator.AND, present(B), present(C))
    base = counted(T, complete, k, B, C)
    cells = ir.WithColumn(base, "PERCENT", percentage("_counted", k))
    rows = ir.WithColumn(cells, "PCT_ROW", percentage("_counted", k, B))
    columns = ir.WithColumn(rows, "PCT_COL", percentage("_counted", k, C))
    assert dataset.source == ir.Sort(
        ir.DropColumns(columns, ("_counted",)),
        (ascending("k"), ascending("b"), ascending("c")),
    )


def test_the_helper_column_gets_a_free_name() -> None:
    [dataset] = translate(
        "proc freq data=t; tables _counted*_COUNTED_2 / out=s; run;"
    ).datasets

    assert isinstance(dataset.source, ir.Sort)
    assert dataset.source.source == ir.DropColumns(
        dataset.source.source.source, ("_counted_3",)
    )


def test_each_tables_statement_with_out_creates_a_data_set() -> None:
    first, second = translate(
        "proc freq data=t; tables a / out=one; tables b; tables c / out=two; run;"
    ).datasets

    assert (first.name, second.name) == ("one", "two")


def test_proc_freq_reads_the_last_data_set_and_a_where_statement() -> None:
    first, second = translate(
        "data a; set t; run; proc freq; where x > 1; by descending k; "
        "tables a / out=s(keep=a count); run;"
    ).datasets

    assert isinstance(second.source, ir.Project)
    sorted_ = second.source.source
    assert isinstance(sorted_, ir.Sort)
    assert sorted_.keys == (descending("k"), ascending("a"))
    dropped = sorted_.source
    assert isinstance(dropped, ir.DropColumns)
    percent = dropped.source
    assert isinstance(percent, ir.WithColumn)
    assert isinstance(percent.source, ir.WithColumn)
    assert isinstance(percent.source.source, ir.Aggregate)
    assert percent.source.source.source == ir.Filter(first, GREATER_1)


@pytest.mark.parametrize(
    ("program", "message"),
    [
        ("proc freq data=t; tables a; run;", "PROC FREQ without an OUT= data set"),
        ("proc freq; tables a / out=s; run;", "PROC FREQ without DATA="),
        (
            "proc freq data=t; tables a / out=perm.s; run;",
            "PROC FREQ writing to library PERM",
        ),
        (
            "proc freq data=t; tables a / out=s outpct; run;",
            "OUTPCT for a table that is not two-way",
        ),
        (
            "proc freq data=t; tables a*b*c / out=s outpct; run;",
            "OUTPCT for a table that is not two-way",
        ),
        (
            "proc freq data=t; tables (a b)*c / out=s; run;",
            "table request with parentheses or a variable list",
        ),
    ],
)
def test_unsupported_proc_freq(program: str, message: str) -> None:
    assert issues(program) == [message]


# --- PROC SQL ------------------------------------------------------------------


def sql(query: str) -> ir.Relation:
    [dataset] = translate(f"proc sql; create table out as {query}; quit;").datasets
    assert dataset.name == "out"
    return dataset.source


def sql_warnings(query: str) -> list[str]:
    translation = translate(f"proc sql; create table out as {query}; quit;")
    return [warning.message for warning in translation.warnings]


def sql_issues(query: str) -> list[str]:
    return issues(f"proc sql; create table out as {query}; quit;")


def test_a_query_follows_sas_rules_for_missing_values() -> None:
    relation = sql("select x, y * 2 as twice from t where x > 1")

    twice = ir.Alias(BinaryOp(BinaryOperator.MULTIPLY, Y, Literal(2)), "twice")
    assert relation == ir.Project(ir.Filter(T, GREATER_1), (X, twice))


def test_select_star_keeps_every_column() -> None:
    assert sql("select * from t where x > 1") == ir.Filter(T, GREATER_1)


def test_a_join_matches_missing_values_with_a_warning() -> None:
    relation = sql(
        "select c.name, o.amount from customers c left join orders as o on c.id = o.id"
    )

    customers = ir.RelationAlias(TableScan(("customers",)), "c")
    orders = ir.RelationAlias(TableScan(("orders",)), "o")
    condition = ir.NullSafeEqual(Column(("c", "id")), Column(("o", "id")))
    joined = ir.Join(customers, orders, ir.JoinKind.LEFT, condition)
    assert relation == ir.Project(
        joined, (Column(("c", "name")), Column(("o", "amount")))
    )
    assert sql_warnings("select * from customers c join orders o on c.id ne o.id") == [
        "equality of two columns"
    ]


def test_a_qualified_column_names_its_table() -> None:
    assert sql("select t.x from t") == ir.Project(
        ir.RelationAlias(T, "t"), (Column(("t", "x")),)
    )


def test_group_by_with_summary_functions_and_having() -> None:
    relation = sql(
        "select a, count(*) as n, sum(x) as total from t group by a having count(*) > 1"
    )

    count = ir.AggregateCall(ir.AggregateFunction.COUNT)
    having = BinaryOp(
        BinaryOperator.AND,
        IsNull(count, negated=True),
        BinaryOp(BinaryOperator.GREATER, count, Literal(1)),
    )
    summary = ir.Aggregate(
        T,
        (A,),
        (
            ir.Alias(count, "n"),
            ir.Alias(aggregate(ir.AggregateFunction.SUM, X), "total"),
            ir.Alias(having, "_having"),
        ),
    )
    assert relation == ir.Project(
        ir.Filter(summary, Column(("_having",))),
        (A, Column(("n",)), Column(("total",))),
    )


def test_a_summary_of_all_rows() -> None:
    assert sql("select count(*) as n, avg(x) as mean from t") == ir.Aggregate(
        T,
        (),
        (
            ir.Alias(ir.AggregateCall(ir.AggregateFunction.COUNT), "n"),
            ir.Alias(aggregate(ir.AggregateFunction.AVG, X), "mean"),
        ),
    )


def test_selected_columns_come_in_the_select_order() -> None:
    relation = sql("select count(distinct x) as n, a from t group by a")

    count = ir.AggregateCall(ir.AggregateFunction.COUNT, (X,), distinct=True)
    assert relation == ir.Project(
        ir.Aggregate(T, (A,), (ir.Alias(count, "n"),)), (Column(("n",)), A)
    )


@pytest.mark.parametrize(
    ("function", "expected"),
    [
        (
            "sum(distinct x)",
            ir.AggregateCall(ir.AggregateFunction.SUM, (X,), distinct=True),
        ),
        ("nmiss(x)", aggregate(ir.AggregateFunction.COUNT_IF, IsNull(X))),
        ("freq(x)", aggregate(ir.AggregateFunction.COUNT, X)),
        ("std(x)", aggregate(ir.AggregateFunction.STDDEV_SAMP, X)),
    ],
)
def test_summary_functions(function: str, expected: ir.Expression) -> None:
    relation = sql(f"select {function} as v from t")

    assert isinstance(relation, ir.Aggregate)
    assert relation.aggregates == (ir.Alias(expected, "v"),)


def test_sum_of_two_columns_is_computed_for_each_row() -> None:
    relation = sql("select sum(x, y) as s from t")

    total = ir.Case(
        (
            (
                IsNull(FunctionCall("coalesce", (X, Y)), negated=True),
                plus(coalesced(X), coalesced(Y)),
            ),
        ),
        None,
    )
    assert relation == ir.Project(T, (ir.Alias(total, "s"),))


def test_order_by_name_position_and_distinct() -> None:
    relation = sql("select distinct a, x from t order by 2 desc, a")

    assert relation == ir.Sort(
        ir.Distinct(ir.Project(T, (A, X))), (descending("x"), ascending("a"))
    )
    assert sql("select * from t order by x") == ir.Sort(T, (ascending("x"),))


def test_create_table_options_and_data_set_options() -> None:
    [dataset] = translate(
        "proc sql; create table out(drop=x) as select a, x from t(where=(x > 1)); quit;"
    ).datasets

    assert dataset.source == ir.DropColumns(
        ir.Project(ir.Filter(T, GREATER_1), (A, X)), ("x",)
    )


def test_several_tables_in_one_step_and_a_later_data_step() -> None:
    first, second, third = translate(
        "proc sql; create table a as select x from t; "
        "create table b as select x from a; quit; data c; set b; run;"
    ).datasets

    assert second.source == ir.Project(first, (X,))
    assert third.source is second


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("select x + 1 from t", "computed column without a name"),
        ("select a, x from t group by a", "column x neither grouped nor summarized"),
        ("select a, sum(x) as s from t", "column a neither grouped nor summarized"),
        ("select x from t where sum(x) > 1", "summary function in WHERE"),
        (
            "select x from t having x > 1",
            "HAVING without GROUP BY or a summary function",
        ),
        (
            "select a, sum(x) as s from t group by a having x > 1",
            "HAVING on a column neither grouped nor summarized",
        ),
        (
            "select a from t order by x",
            "ORDER BY something other than a selected column",
        ),
        (
            "select a from t order by 2",
            "ORDER BY something other than a selected column",
        ),
        ("select a, x as a from t", "two columns named a"),
        ("select *, x from t", "* with other columns"),
        ("select * from t group by a", "* with GROUP BY or a summary function"),
        ("select sum(x) as s from t group by x + 1", "GROUP BY of an expression"),
        ("select avg(distinct x) as m from t", "AVG(DISTINCT ...)"),
        ("select sum(*) as s from t", "SUM(*)"),
        ("select sum(.) as s from t", "SUM of a missing value"),
        (
            "select max(sum(x)) as m from t",
            "summary function inside a summary function",
        ),
        ("select x from t where x < y", "comparison of two variables"),
        ("select x from t join u on t.a = scan(u.b, 1)", "function SCAN"),
        ("select scan(x, 1) as s from t", "function SCAN"),
        ("select a, count(*) as a from t group by a", "two columns named a"),
        ("select sum(scan(x, 1)) as s from t", "function SCAN"),
        (
            "select a, sum(x) as s from t group by a having scan(a) = 'b'",
            "function SCAN",
        ),
    ],
)
def test_unsupported_proc_sql(query: str, message: str) -> None:
    assert sql_issues(query) == [message]


def test_proc_sql_into_a_permanent_library() -> None:
    assert issues("proc sql; create table perm.out as select x from t; quit;") == [
        "CREATE TABLE in library PERM"
    ]
    assert issues(
        "proc sql; create table out(where=(x > 1)) as select x from t; quit;"
    ) == ["WHERE= option on the output data set"]


@pytest.mark.parametrize(
    "having",
    [
        "not (sum(x) > 1)",
        "count(*) in (1, 2)",
        "sum(x) between 1 and 5",
        "sum(x) is missing",
    ],
)
def test_summary_functions_in_any_condition(having: str) -> None:
    relation = sql(f"select a, count(*) as n from t group by a having {having}")

    assert isinstance(relation, ir.Project)
    assert isinstance(relation.source, ir.Filter)


@pytest.mark.parametrize(
    "where",
    ["not (t.x = 1)", "t.x in (1, 2)", "t.x between 1 and 2", "t.x is missing"],
)
def test_qualified_columns_in_any_condition(where: str) -> None:
    relation = sql(f"select x from t where {where}")

    assert isinstance(relation, ir.Project)
    assert isinstance(relation.source, ir.Filter)
    assert relation.source.source == ir.RelationAlias(T, "t")


@pytest.mark.parametrize(
    "item",
    ["-a", "a in (1, 2)", "a between 1 and 2", "a is missing"],
)
def test_grouped_columns_in_any_expression(item: str) -> None:
    relation = sql(f"select {item} as v, count(*) as n from t group by a")

    # The grouped column a is not selected, so a select drops it.
    assert isinstance(relation, ir.Project)
    assert isinstance(relation.source, ir.Aggregate)
    assert relation.source.keys == (A,)


def test_a_column_inside_an_expression_must_be_grouped() -> None:
    assert sql_issues("select (x between 1 and 2) as v, count(*) as n from t") == [
        "column x neither grouped nor summarized"
    ]


@pytest.mark.parametrize(
    ("program", "message"),
    [
        (
            "data a; set t; y = count(*); run;",
            "COUNT(*) outside a query's SELECT or HAVING",
        ),
        (
            "data a; set t; y = count(distinct x); run;",
            "COUNT(DISTINCT) outside a query's SELECT or HAVING",
        ),
        ("data a; set t; y = t.x; run;", "qualified name"),
    ],
)
def test_sql_constructs_in_a_data_step(program: str, message: str) -> None:
    assert issues(program) == [message]
