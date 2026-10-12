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
        ("upcase(x) = 'A'", "function UPCASE"),
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
        ("x = 1 and upcase(x) = 'A'", "function UPCASE"),
        ("upcase(x) = 'A' or x = 1", "function UPCASE"),
        ("not (upcase(x) = 'A')", "function UPCASE"),
        ("x in (upcase(y))", "function UPCASE"),
        ("upcase(x) in (1)", "function UPCASE"),
        ("upcase(x) is missing", "function UPCASE"),
        ("x between upcase(a) and 'b'", "function UPCASE"),
        ("x = upcase(y)", "function UPCASE"),
        ("upcase(y) + 1 > 0", "function UPCASE"),
        ("x * upcase(y) > 0", "function UPCASE"),
        ("-upcase(y) > 0", "function UPCASE"),
        ("upcase(y)", "function UPCASE"),
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
    assert issues("data a; where upcase(x) = 'A'; total = y ** 2; run;") == [
        "DATA step without a SET statement",
        "function UPCASE",
        "the ** operator",
    ]


def test_every_issue_in_the_program_is_reported() -> None:
    assert issues(
        "proc sort data=a; by x; run;\n"
        "data b; set a; where x = y; total = upcase(z); run;\n"
        "data _null_; set b; run;"
    ) == [
        "PROC SORT step",
        "comparison of two variables",
        "function UPCASE",
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

    helper = Column(("_if_1",))
    assert relation == ir.DropColumns(
        ir.WithColumn(
            ir.WithColumn(
                ir.WithColumn(ir.Project(T, (X,)), "_if_1", GREATER_1),
                "x",
                ir.Case(((helper, Literal(0)),), X),
            ),
            "y",
            ir.Case(((helper, Literal(1)),)),
        ),
        ("_if_1",),
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
        ("y = upcase(x);", "function UPCASE"),
        ("y = 'a' * 2;", "arithmetic on text"),
        ("y = x = z;", "comparison of two variables"),
        ("if y = 1 then x = upcase(z); else x = 1;", "function UPCASE"),
        ("if upcase(z) = 'A' then x = 1; else x = 2;", "function UPCASE"),
        ("if upcase(z) = 'A' then do; x = 1; end; else x = 2;", "function UPCASE"),
        (
            "if x then y = 1; else y = 2;",
            "a variable whose type SparkShift cannot see as a condition",
        ),
        ("if x > 1;  if upcase(z);", "function UPCASE"),
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
