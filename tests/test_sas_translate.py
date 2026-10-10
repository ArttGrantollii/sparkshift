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
        ("x + 1 > 2", "arithmetic in a condition"),
        ("upcase(x) = 'A'", "function UPCASE in a condition"),
        ("x || 'a' = 'b'", "concatenation in a condition"),
        ("-x = 1", "arithmetic in a condition"),
        ("x", "a variable as a condition"),
        ("1", "a constant as a condition"),
        ("x + 1", "arithmetic as a condition"),
        ("1 in (1)", "IN on a constant"),
        ("x in (y)", "variable in an IN list"),
        ("x in ('a', 1)", "IN list of text and numbers"),
        ("1 between 0 and 2", "BETWEEN other than a variable between two constants"),
        ("x between . and 2", "BETWEEN other than a variable between two constants"),
        ("x between 1 and 'b'", "BETWEEN a number and text"),
        ("1 is missing", "IS MISSING on a constant"),
        ("x = 1 and upcase(x) = 'A'", "function UPCASE in a condition"),
        ("upcase(x) = 'A' or x = 1", "function UPCASE in a condition"),
        ("not (upcase(x) = 'A')", "function UPCASE in a condition"),
        ("x in (upcase(y))", "function UPCASE in a condition"),
        ("upcase(x) is missing", "function UPCASE in a condition"),
        ("x between upcase(a) and 'b'", "function UPCASE in a condition"),
        ("(x = 1) = 1", "an expression in a condition"),
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
    assert issues("data a; where upcase(x) = 'A'; run;") == [
        "DATA step without a SET statement",
        "function UPCASE in a condition",
    ]


def test_every_issue_in_the_program_is_reported() -> None:
    assert issues(
        "proc sort data=a; by x; run;\n"
        "data b; set a; where x = y; total = 1; run;\n"
        "data _null_; set b; run;"
    ) == [
        "PROC SORT step",
        "assignment statement",
        "comparison of two variables",
        "DATA _NULL_ step",
    ]


def test_issues_carry_the_source_and_a_hint() -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        translate("data a; set b;\n  where x =\n    y; run;")

    [issue] = caught.value.issues
    assert issue.sql == "x = y"
    assert issue.hint is not None and "Compare a variable with a constant" in issue.hint
