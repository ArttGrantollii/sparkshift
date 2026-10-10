"""Tests for parsing SAS programs."""

import dataclasses

import pytest

from sparkshift.errors import SASParseError
from sparkshift.sas_parser import (
    Dataset,
    DataStep,
    DropOption,
    DropStatement,
    KeepOption,
    KeepStatement,
    Program,
    RenameOption,
    RenameStatement,
    SetStatement,
    Span,
    WhereOption,
    WhereStatement,
    parse_sas,
)


def shape(node: object) -> object:
    """A syntax tree as nested tuples, without source positions."""
    if isinstance(node, Span):
        return None
    if dataclasses.is_dataclass(node) and not isinstance(node, type):
        fields = [
            shape(getattr(node, f.name))
            for f in dataclasses.fields(node)
            if f.name != "span"
        ]
        return (type(node).__name__, *fields)
    if isinstance(node, tuple):
        return tuple(shape(item) for item in node)
    return node


def issues(program: Program) -> list[str]:
    return [issue.message for issue in program.issues]


def where(condition: str) -> tuple[object, list[str]]:
    """The shape of a WHERE condition, and the issues found."""
    program = parse_sas(f"data a; set b; where {condition}; run;")
    statement = program.steps[0].statements[1]
    assert isinstance(statement, WhereStatement)
    return shape(statement.condition), issues(program)


def name(text: str) -> tuple[str, str]:
    return ("Name", text)


def number(value: int | float) -> tuple[str, int | float]:
    return ("Number", value)


def string(value: str) -> tuple[str, str]:
    return ("String", value)


def binary(op: str, left: object, right: object) -> tuple[str, str, object, object]:
    return ("Binary", op, left, right)


# --- Steps -------------------------------------------------------------------


def test_a_data_step() -> None:
    program = parse_sas("data work.out; set sales.customers; run;")

    assert program.issues == ()
    assert shape(program.steps) == (
        (
            "DataStep",
            (("Dataset", "work", "out", ()),),
            (("SetStatement", (("Dataset", "sales", "customers", ()),)),),
        ),
    )


@pytest.mark.parametrize(
    "source",
    [
        "data a; set b; data c; set a; run;",  # the next DATA statement ends a step
        "data a; set b; run; data c; set a;",  # so does the end of the program
        "DATA a; SET b; RUN; Data c; Set a; Run;",  # keywords ignore case
    ],
)
def test_steps_end_at_run_the_next_step_or_the_end(source: str) -> None:
    program = parse_sas(source)

    assert [step.outputs[0].member for step in program.steps] == ["a", "c"]
    assert all(len(step.statements) == 1 for step in program.steps)


def test_a_data_step_ends_at_a_proc_step() -> None:
    program = parse_sas("data a; set b; proc print data=a; run;")

    assert len(program.steps[0].statements) == 1
    assert issues(program) == ["PROC PRINT step"]


def test_proc_steps_are_reported_and_skipped() -> None:
    program = parse_sas(
        "proc sort data=a out=b; by x; run; "
        "proc sql; select 1; quit; "
        "data c; set b; run;"
    )

    assert issues(program) == ["PROC SORT step", "PROC SQL step"]
    assert [step.outputs[0].member for step in program.steps] == ["c"]


def test_a_proc_step_may_end_at_the_next_step_or_the_end() -> None:
    program = parse_sas("proc print data=a; data c; set b; proc print data=c;")

    assert issues(program) == ["PROC PRINT step", "PROC PRINT step"]
    assert len(program.steps) == 1


def test_global_statements_and_macros_are_reported() -> None:
    program = parse_sas(
        "options nodate; libname sales '/data'; %let cutoff = 3; title 'x'; "
        "data a; set b; run; run;"
    )

    assert issues(program) == [
        "OPTIONS statement",
        "LIBNAME statement",
        "macro %let",
        "TITLE statement",
    ]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("", "No DATA or PROC step found"),
        ("/* only a comment */ ;;", "No DATA or PROC step found"),
        ("data a; set b; run", "Statement without a closing semicolon"),
        ("proc;", "PROC without a procedure name"),
    ],
)
def test_invalid_programs(source: str, message: str) -> None:
    with pytest.raises(SASParseError, match=message):
        parse_sas(source)


def test_a_program_of_unsupported_statements_still_parses() -> None:
    program = parse_sas("options nodate;")

    assert program.steps == ()
    assert issues(program) == ["OPTIONS statement"]


# --- Statements --------------------------------------------------------------


def test_data_step_statements() -> None:
    program = parse_sas(
        "data a; set b; where x = 1; keep x y; drop z; rename x = new_x y = new_y; run;"
    )
    statements = program.steps[0].statements

    assert [type(s) for s in statements] == [
        SetStatement,
        WhereStatement,
        KeepStatement,
        DropStatement,
        RenameStatement,
    ]
    keep, drop, rename = statements[2], statements[3], statements[4]
    assert isinstance(keep, KeepStatement) and keep.names == ("x", "y")
    assert isinstance(drop, DropStatement) and drop.names == ("z",)
    assert isinstance(rename, RenameStatement)
    assert rename.pairs == (("x", "new_x"), ("y", "new_y"))


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("total = a + b", "assignment statement"),
        ("if x > 1 then output", "IF statement"),
        ("retain total", "RETAIN statement"),
        ("by customer_id", "BY statement"),
        ("&code", "macro &code"),
    ],
)
def test_other_data_step_statements_are_reported(statement: str, message: str) -> None:
    program = parse_sas(f"data a; set b; {statement}; run;")

    assert issues(program) == [message]
    assert len(program.steps[0].statements) == 1


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("data a; set b; rename; run;", "RENAME names no variables"),
        ("data a; set b; rename x = ; run;", "Expected the variable's new name"),
        ("data a; set b; keep; run;", "KEEP names no variables"),
        ("data a; set b; where x = 1 2; run;", "Unexpected '2'"),
        (
            "data a; set b; rename a = b c; run;",
            "Expected '=', found 'end of statement'",
        ),
        ("data a; set b(keep a); run;", "Expected '=', found 'a'"),
        ("data a; set 3; run;", "Expected a data set name"),
        ("data a; set lib.; run;", "Expected a data set name after the period"),
        ("data a; set b c=1 d; run;", "Unexpected 'd' in the SET statement"),
        ("data a; set b(label=(x y; run;", "Expected a data set option"),
    ],
)
def test_invalid_statements(source: str, message: str) -> None:
    with pytest.raises(SASParseError, match=message):
        parse_sas(source)


# --- Data sets and options ---------------------------------------------------


def dataset(source: str) -> Dataset:
    program = parse_sas(f"data a; set {source}; run;")
    statement = program.steps[0].statements[0]
    assert isinstance(statement, SetStatement)
    return statement.datasets[0]


def test_a_library_and_member() -> None:
    assert (dataset("sales.customers").library, dataset("sales.customers").member) == (
        "sales",
        "customers",
    )
    assert dataset("customers").library is None


def test_data_set_options() -> None:
    found = dataset(
        "orders(keep=order_id amount drop=x rename=(amount=total id=key) "
        "where=(amount > 100))"
    )

    keep, drop, rename, where_option = found.options
    assert isinstance(keep, KeepOption) and keep.names == ("order_id", "amount")
    assert isinstance(drop, DropOption) and drop.names == ("x",)
    assert isinstance(rename, RenameOption)
    assert rename.pairs == (("amount", "total"), ("id", "key"))
    assert isinstance(where_option, WhereOption)
    assert shape(where_option.condition) == binary(">", name("amount"), number(100))


def test_unknown_options_are_reported_and_skipped() -> None:
    program = parse_sas(
        "data a(compress=yes); "
        "set b(firstobs=2 obs=10 keep=x label=(a b)) end=last; "
        "run;"
    )

    assert issues(program) == [
        "data set option COMPRESS=",
        "data set option FIRSTOBS=",
        "data set option OBS=",
        "data set option LABEL=",
        "SET option END=",
    ]
    statement = program.steps[0].statements[0]
    assert isinstance(statement, SetStatement)
    assert [type(o) for o in statement.datasets[0].options] == [KeepOption]


@pytest.mark.parametrize(
    "names", ["x1-x5", "a--c", "prefix:", "_all_", "_numeric_", "_character_"]
)
def test_variable_lists_are_reported(names: str) -> None:
    program = parse_sas(f"data a; set b; keep {names} y; run;")

    assert issues(program) == ["variable list in KEEP"]
    keep = program.steps[0].statements[1]
    assert isinstance(keep, KeepStatement) and keep.names == ("y",)


def test_a_variable_list_alone_is_not_an_empty_keep() -> None:
    program = parse_sas("data a; set b(drop=x1-x3); run;")

    assert issues(program) == ["variable list in DROP="]


def test_several_output_data_sets() -> None:
    program = parse_sas("data a b(keep=x); set c; run;")

    assert [d.member for d in program.steps[0].outputs] == ["a", "b"]


def test_a_step_without_statements() -> None:
    [step] = parse_sas("data a; run;").steps

    assert step == DataStep(step.outputs, (), step.span)


# --- Expressions -------------------------------------------------------------


@pytest.mark.parametrize(
    ("operator", "op"),
    [
        ("=", "="), ("eq", "="), ("^=", "^="), ("~=", "^="), ("¬=", "^="),
        ("ne", "^="), ("<", "<"), ("lt", "<"), ("<=", "<="), ("=<", "<="),
        ("le", "<="), (">", ">"), ("GT", ">"), (">=", ">="), ("=>", ">="),
        ("ge", ">="),
    ],
)  # fmt: skip
def test_comparison_operators(operator: str, op: str) -> None:
    assert where(f"x {operator} 1") == (binary(op, name("x"), number(1)), [])


def test_and_binds_more_tightly_than_or() -> None:
    a, b, c = (binary("=", name(v), number(1)) for v in "abc")

    assert where("a = 1 or b = 1 and c = 1") == (
        binary("OR", a, binary("AND", b, c)),
        [],
    )
    assert where("a = 1 & b = 1 | c = 1") == (binary("OR", binary("AND", a, b), c), [])


def test_symbols_for_or() -> None:
    assert where("a = 1 ! b = 1")[0] == where("a = 1 or b = 1")[0]


def test_parentheses_group() -> None:
    a, b, c = (binary("=", name(v), number(1)) for v in "abc")

    assert where("(a = 1 or b = 1) and c = 1")[0] == binary(
        "AND", binary("OR", a, b), c
    )


@pytest.mark.parametrize("not_word", ["not", "^", "~", "¬"])
def test_not_with_parentheses(not_word: str) -> None:
    assert where(f"{not_word} (x = 1)") == (
        ("Unary", "NOT", binary("=", name("x"), number(1))),
        [],
    )


def test_not_without_parentheses_is_reported_once() -> None:
    condition, found = where("not x = 1")

    assert found == ["NOT without parentheses"]
    assert condition == ("Unary", "NOT", binary("=", name("x"), number(1)))


def test_in_lists() -> None:
    expected = ("In", name("x"), (number(1), number(2), ("Missing",)), False)

    assert where("x in (1, 2, .)") == (expected, [])
    assert where("x in (1 2 .)") == (expected, [])
    assert where("x not in ('a', 'b')") == (
        ("In", name("x"), (string("a"), string("b")), True),
        [],
    )
    assert where("x in (-1)")[0] == (
        "In",
        name("x"),
        (("Unary", "-", number(1)),),
        False,
    )


def test_between() -> None:
    assert where("x between 1 and 5") == (
        ("Between", name("x"), number(1), number(5), False),
        [],
    )
    assert where("x not between 'a' and 'm' and y = 2")[0] == binary(
        "AND",
        ("Between", name("x"), string("a"), string("m"), True),
        binary("=", name("y"), number(2)),
    )


@pytest.mark.parametrize(
    ("condition", "negated"),
    [
        ("x is missing", False),
        ("x is null", False),
        ("x is not missing", True),
        ("x IS NOT NULL", True),
    ],
)
def test_is_missing(condition: str, negated: bool) -> None:
    assert where(condition) == (("IsMissing", name("x"), negated), [])


def test_arithmetic_and_calls() -> None:
    assert where("a + b * -c ** 2 > f(x, 'y') || z")[0] == binary(
        ">",
        binary(
            "+",
            name("a"),
            binary("*", name("b"), ("Unary", "-", binary("**", name("c"), number(2)))),
        ),
        binary("||", ("Call", "f", (name("x"), string("y"))), name("z")),
    )
    assert where("x = f()")[0] == binary("=", name("x"), ("Call", "f", ()))
    assert where("x = a / 2 - +b")[0] == binary(
        "=",
        name("x"),
        binary("-", binary("/", name("a"), number(2)), ("Unary", "+", name("b"))),
    )


def test_numbers_are_ints_or_floats() -> None:
    assert where("x = 3")[0] == binary("=", name("x"), number(3))
    assert where("x = 2.5")[0] == binary("=", name("x"), number(2.5))
    assert where("x = 1e2")[0] == binary("=", name("x"), number(100.0))


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ("a < b < c", "chained comparison"),
        ("a <> b", "the <> operator"),
        ("a >< b", "the >< operator"),
        ("name =: 'S'", "comparison with a colon modifier"),
        ("x in (1:5)", "range in an IN list"),
        ("d > '01jan2024'd", "D literal"),
        ('name = "&who"', "macro reference in a string"),
        ("x = .a", "special missing value"),
        ("x = &cutoff", "macro &cutoff"),
    ],
)
def test_unsupported_expressions_are_reported(condition: str, message: str) -> None:
    assert where(condition)[1] == [message]


def test_single_quotes_never_resolve_macros() -> None:
    assert where("name = '&who'") == (binary("=", name("name"), string("&who")), [])


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ("x is 1", "IS must be followed by MISSING or NULL"),
        ("x between 1", "BETWEEN without AND"),
        ("x in ()", "IN with an empty list"),
        ("x = f(a b)", "Expected , or \\) in a function call"),
        ("x = (1", "Expected '\\)'"),
        ("x = )", "Unexpected '\\)' in an expression"),
        ("x =", "Unexpected 'end of statement' in an expression"),
    ],
)
def test_invalid_expressions(condition: str, message: str) -> None:
    with pytest.raises(SASParseError, match=message):
        parse_sas(f"data a; set b; where {condition}; run;")


def test_issues_quote_the_source_on_one_line() -> None:
    program = parse_sas("data a; set b;\n  if x\n    then output;\nrun;")

    assert [issue.sql for issue in program.issues] == ["if x then output"]


def test_spans_point_at_the_source() -> None:
    source = "data a;\n  set b;\n  where (x = 1);\nrun;"
    statement = parse_sas(source).steps[0].statements[1]
    assert isinstance(statement, WhereStatement)
    span = statement.condition.span

    assert source[span.start : span.end] == "(x = 1)"
    assert (span.line, span.column) == (3, 9)
