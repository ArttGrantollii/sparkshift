"""Tests for parsing SAS programs."""

import dataclasses

import pytest

from sparkshift.errors import SASParseError
from sparkshift.sas_parser import (
    DatalinesStatement,
    Dataset,
    DataStep,
    DropOption,
    DropStatement,
    FormatStatement,
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
    program = parse_sas("data a; set b; proc contents data=a; run;")

    assert len(program.steps[0].statements) == 1
    assert issues(program) == ["PROC CONTENTS step"]


def test_proc_steps_are_reported_and_skipped() -> None:
    program = parse_sas(
        "proc transpose data=a out=b; by x; run; "
        "proc report data=b; column x; quit; "
        "data c; set b; run;"
    )

    assert issues(program) == ["PROC TRANSPOSE step", "PROC REPORT step"]
    assert [step.outputs[0].member for step in program.steps] == ["c"]


def test_a_proc_step_may_end_at_the_next_step_or_the_end() -> None:
    program = parse_sas("proc contents data=a; data c; set b; proc contents data=c;")

    assert issues(program) == ["PROC CONTENTS step", "PROC CONTENTS step"]
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
        ("output", "OUTPUT statement"),
        ("if x > 1 then output", "OUTPUT statement"),
        ("if x > 1 then x = 1; else output", "OUTPUT statement"),
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
        binary("||", ("Call", "f", (name("x"), string("y")), False, False), name("z")),
    )
    assert where("x = f()")[0] == binary(
        "=", name("x"), ("Call", "f", (), False, False)
    )
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
    program = parse_sas("data a; set b;\n  retain\n    total 0;\nrun;")

    assert [issue.sql for issue in program.issues] == ["retain total 0"]


def test_spans_point_at_the_source() -> None:
    source = "data a;\n  set b;\n  where (x = 1);\nrun;"
    statement = parse_sas(source).steps[0].statements[1]
    assert isinstance(statement, WhereStatement)
    span = statement.condition.span

    assert source[span.start : span.end] == "(x = 1)"
    assert (span.line, span.column) == (3, 9)


# --- Program statements ------------------------------------------------------


def statements(body: str) -> tuple[object, ...]:
    """The shapes of a DATA step's statements after SET."""
    program = parse_sas(f"data a; set b; {body} run;")
    assert program.issues == ()
    return tuple(shape(s) for s in program.steps[0].statements[1:])


def assign(variable: str, value: object) -> tuple[str, str, object]:
    return ("Assignment", variable, value)


def test_assignments() -> None:
    assert statements("net = amount * (1 - discount); flag = .;") == (
        assign(
            "net", binary("*", name("amount"), binary("-", number(1), name("discount")))
        ),
        assign("flag", ("Missing",)),
    )


def test_subsetting_if_and_delete() -> None:
    assert statements("if x > 1; delete;") == (
        ("SubsettingIf", binary(">", name("x"), number(1))),
        ("Delete",),
    )


def test_if_then_else_if_else() -> None:
    [chain] = statements(
        "if x > 1 then size = 'large'; else if x > 0 then size = 'small'; "
        "else size = 'none';"
    )

    assert chain == (
        "IfThen",
        binary(">", name("x"), number(1)),
        assign("size", string("large")),
        (
            "IfThen",
            binary(">", name("x"), number(0)),
            assign("size", string("small")),
            assign("size", string("none")),
        ),
    )


def test_if_then_without_else_and_with_delete() -> None:
    assert statements("if x then delete; y = 1;") == (
        ("IfThen", name("x"), ("Delete",), None),
        assign("y", number(1)),
    )


def test_do_blocks_nest() -> None:
    [block] = statements(
        "if x then do; a = 1; if y then do; b = 2; end; end; else do; a = 0; end;"
    )

    assert block == (
        "IfThen",
        name("x"),
        (
            "Block",
            (
                assign("a", number(1)),
                ("IfThen", name("y"), ("Block", (assign("b", number(2)),)), None),
            ),
        ),
        ("Block", (assign("a", number(0)),)),
    )
    assert statements("do; a = 1; end;") == (("Block", (assign("a", number(1)),)),)


def test_lengths_apply_to_the_names_before_them() -> None:
    assert statements("length a b $ 10 n 8 c $3;") == (
        (
            "LengthStatement",
            (("a", True, 10), ("b", True, 10), ("n", False, 8), ("c", True, 3)),
        ),
    )


def test_unsupported_branches_and_loops_are_reported() -> None:
    program = parse_sas(
        "data a; set b; if x then output; else y = 1; "
        "do i = 1 to 3; total = i; end; do while (x < 3); x = x + 1; end; "
        "if y then z = 1; else output; run;"
    )

    assert issues(program) == [
        "OUTPUT statement",
        "DO loop",
        "DO loop",
        "OUTPUT statement",
    ]
    # Only the SET statement is left.
    assert len(program.steps[0].statements) == 1


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("else x = 1;", "ELSE without a matching IF"),
        ("end;", "END without a matching DO"),
        ("if x y = 1;", "Expected THEN, found 'y'"),
        ("if x then;", "THEN without a statement"),
        ("if x then y = 1; else;", "ELSE without a statement"),
        ("if x then do; y = 1;", "DO without a matching END"),
        ("length a;", "LENGTH without a length"),
        ("length $ 10;", "Expected a variable name or a length"),
        ("length a $ b;", "Expected a variable name or a length"),
        ("length a 1.5;", "Expected a variable name or a length"),
        ("x = 1 2;", "Unexpected '2'"),
    ],
)
def test_invalid_program_statements(body: str, message: str) -> None:
    with pytest.raises(SASParseError, match=message):
        parse_sas(f"data a; set b; {body} run;")


@pytest.mark.parametrize(
    "source", ["data a; set b; do; x = 1; run;", "data a; set b; do; x = 1;"]
)
def test_a_do_block_must_end_before_the_step_or_program(source: str) -> None:
    with pytest.raises(SASParseError, match="DO without a matching END"):
        parse_sas(source)


def test_an_if_may_end_the_program() -> None:
    [step] = parse_sas("data a; set b; if x then y = 1;").steps

    assert shape(step.statements[-1]) == (
        "IfThen",
        name("x"),
        assign("y", number(1)),
        None,
    )


def test_unsupported_statements_in_a_block_are_reported() -> None:
    program = parse_sas("data a; set b; do; output; y = 1; end; run;")

    assert issues(program) == ["OUTPUT statement"]
    assert shape(program.steps[0].statements[-1]) == (
        "Block",
        (assign("y", number(1)),),
    )


def test_an_of_variable_list_is_reported_not_rejected() -> None:
    program = parse_sas("data a; set b; total = sum(of x1-x3 y) + 1; run;")

    assert issues(program) == ["OF variable list"]
    assert program.issues[0].sql == "sum(of x1-x3 y)"


def test_an_unclosed_of_variable_list_is_an_error() -> None:
    with pytest.raises(SASParseError, match="Expected \\) to end the function call"):
        parse_sas("data a; set b; total = sum(of x1 x2; run;")


# --- PROC SORT -----------------------------------------------------------------


def test_proc_sort() -> None:
    program = parse_sas(
        "proc sort data=sales.orders(keep=a b) out=sorted nodupkey; "
        "by a descending b; where a > 1; run;"
    )

    assert program.issues == ()
    assert shape(program.steps) == (
        (
            "ProcSort",
            ("Dataset", "sales", "orders", (("KeepOption", ("a", "b")),)),
            ("Dataset", None, "sorted", ()),
            (("ByVariable", "a", False), ("ByVariable", "b", True)),
            True,
            (("WhereStatement", binary(">", name("a"), number(1))),),
        ),
    )


def test_proc_sort_without_data_or_out() -> None:
    [step] = parse_sas("PROC SORT; BY x; RUN;").steps

    assert shape(step) == (
        "ProcSort",
        None,
        None,
        (("ByVariable", "x", False),),
        False,
        (),
    )


def test_a_variable_named_descending() -> None:
    [step] = parse_sas("proc sort data=a; by descending; run;").steps

    assert shape(step)[3] == (("ByVariable", "descending", False),)


def test_proc_sort_options_that_do_not_change_the_result() -> None:
    program = parse_sas(
        "proc sort data=a force tagsort sortsize=max equals noequals presorted "
        "threads nothreads datecopy overwrite size=1G; by x; run;"
    )

    assert program.issues == ()


def test_unsupported_proc_sort_options_and_statements() -> None:
    program = parse_sas(
        "proc sort data=a dupout=d nouniquekey sortseq=ascii; by x; "
        "format x 8.; key y; run;"
    )

    assert issues(program) == [
        "PROC SORT option DUPOUT=",
        "PROC SORT option NOUNIQUEKEY",
        "PROC SORT option SORTSEQ=",
        "FORMAT statement in PROC SORT",
        "KEY statement in PROC SORT",
    ]
    assert len(program.steps) == 1


def test_a_proc_sort_ends_at_the_next_step() -> None:
    program = parse_sas("proc sort data=a; by x; data b; set a; run;")

    assert [type(step).__name__ for step in program.steps] == ["ProcSort", "DataStep"]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("proc sort data=a; run;", "PROC SORT without a BY statement"),
        ("proc sort data=a; by x; by y; run;", "PROC SORT with a second BY statement"),
        ("proc sort data=a; by; run;", "BY names no variables"),
        ("proc sort data=a; by x-y; run;", "Expected a BY variable"),
        ("proc sort data=a; by descending 1; run;", "Expected a variable after"),
        ("proc sort 'a'; by x; run;", "Expected a PROC SORT option"),
        ("proc sort data=a; by x; where x >; run;", "Unexpected"),
    ],
)
def test_invalid_proc_sort(source: str, message: str) -> None:
    with pytest.raises(SASParseError, match=message):
        parse_sas(source)


# --- PROC MEANS and SUMMARY ----------------------------------------------------


def test_proc_means() -> None:
    program = parse_sas(
        "proc means data=orders(keep=a b c) nway missing noprint maxdec=2 n mean; "
        "by k descending j; class a b; class c / missing order=unformatted; "
        "var x y; where x > 1; "
        "output out=stats(drop=_type_) n= mean(x)=avg sum(x y)=sx sy / noinherit; "
        "run;"
    )

    assert program.issues == ()
    assert shape(program.steps) == (
        (
            "ProcMeans",
            "MEANS",
            ("Dataset", None, "orders", (("KeepOption", ("a", "b", "c")),)),
            True,
            True,
            (("ByVariable", "k", False), ("ByVariable", "j", True)),
            (("a", False), ("b", False), ("c", True)),
            ("x", "y"),
            (
                (
                    "OutputStatement",
                    ("Dataset", None, "stats", (("DropOption", ("_type_",)),)),
                    (
                        ("StatisticRequest", "N", (), ()),
                        ("StatisticRequest", "MEAN", ("x",), ("avg",)),
                        ("StatisticRequest", "SUM", ("x", "y"), ("sx", "sy")),
                    ),
                ),
            ),
            (("WhereStatement", binary(">", name("x"), number(1))),),
        ),
    )


def test_proc_summary_without_options() -> None:
    [step] = parse_sas("proc summary; output out=s; run;").steps

    assert shape(step) == (
        "ProcMeans",
        "SUMMARY",
        None,
        False,
        False,
        (),
        (),
        (),
        (("OutputStatement", ("Dataset", None, "s", ()), ()),),
        (),
    )


def test_proc_means_options_that_do_not_change_the_output() -> None:
    program = parse_sas(
        "proc means data=a print fw=12 nonobs alpha=0.1 vardef=df qmethod=os "
        "sumsize=512M threads clm median p90; run;"
    )

    assert program.issues == ()


def test_unsupported_proc_means_options_and_statements() -> None:
    program = parse_sas(
        "proc means data=a vardef=n chartype order=freq descendtypes; "
        "class a / descending preloadfmt; class b / order=data; var x / weight=w; "
        "types a; id b; by c notsorted; "
        "output out=s idgroup(max(x) out(y)=top) mean=; "
        "output out=t mean= / autoname levels; run;"
    )

    assert issues(program) == [
        "PROC MEANS option VARDEF=N",
        "PROC MEANS option CHARTYPE",
        "PROC MEANS option ORDER=FREQ",
        "PROC MEANS option DESCENDTYPES",
        "CLASS option DESCENDING",
        "CLASS option PRELOADFMT",
        "CLASS option ORDER=DATA",
        "VAR statement options",
        "TYPES statement in PROC MEANS",
        "ID statement in PROC MEANS",
        "BY option NOTSORTED",
        "IDGROUP in an OUTPUT statement",
        "OUTPUT option AUTONAME",
        "OUTPUT option LEVELS",
    ]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("proc means data=a; by x; by y; run;", "PROC MEANS with a second BY"),
        ("proc means data=a; class; run;", "CLASS names no variables"),
        ("proc means data=a; output out=s mean x; run;", "Expected '='"),
        ("proc means data=a; output out=s mean(x=a; run;", "Expected '\\)'"),
        ("proc means data=a; output out=s 'mean'=a; run;", "Expected OUT= or"),
        ("proc means data=a; class x / 'missing'; run;", "Expected a CLASS option"),
        ("proc means data=a; output out=s / 'x'; run;", "Expected an OUTPUT option"),
        ("proc means data=a; where x >; run;", "Unexpected"),
        ("proc means data=a; where x > 1 y; run;", "Unexpected 'y'"),
        ("proc summary 1; run;", "Expected a PROC SUMMARY option"),
    ],
)
def test_invalid_proc_means(source: str, message: str) -> None:
    with pytest.raises(SASParseError, match=message):
        parse_sas(source)


# --- PROC FREQ -----------------------------------------------------------------


def test_proc_freq() -> None:
    program = parse_sas(
        "proc freq data=orders noprint order=internal; by k; where x > 1; "
        "tables a a*b / out=counts(drop=percent) missing outpct nocum list; "
        "table c; run;"
    )

    assert program.issues == ()
    assert shape(program.steps) == (
        (
            "ProcFreq",
            ("Dataset", None, "orders", ()),
            (("ByVariable", "k", False),),
            (
                (
                    "TablesStatement",
                    (("a",), ("a", "b")),
                    ("Dataset", None, "counts", (("DropOption", ("percent",)),)),
                    True,
                    True,
                ),
                ("TablesStatement", (("c",),), None, False, False),
            ),
            (("WhereStatement", binary(">", name("x"), number(1))),),
        ),
    )


def test_unsupported_proc_freq_options_and_statements() -> None:
    program = parse_sas(
        "proc freq data=a order=freq formchar='|'; weight w; "
        "tables a / out=c sparse outcum chisq; tables (a b)*c / out=d; "
        "tables a-c; tables a*b--d; exact chisq; run;"
    )

    assert issues(program) == [
        "PROC FREQ option ORDER=FREQ",
        "PROC FREQ option FORMCHAR='|'",
        "WEIGHT statement in PROC FREQ",
        "TABLES option SPARSE",
        "TABLES option OUTCUM",
        "TABLES option CHISQ",
        "table request with parentheses or a variable list",
        "table request with parentheses or a variable list",
        "table request with parentheses or a variable list",
        "EXACT statement in PROC FREQ",
    ]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("proc freq data=a; by x; by y; run;", "PROC FREQ with a second BY"),
        ("proc freq data=a; tables / out=b; run;", "TABLES names no variables"),
        ("proc freq data=a; tables a*; run;", "Expected a variable after \\*"),
        ("proc freq data=a; tables a / 'out'; run;", "Expected a TABLES option"),
        ("proc freq data=a; where x >; run;", "Unexpected"),
        ("proc freq 'a'; run;", "Expected a PROC FREQ option"),
    ],
)
def test_invalid_proc_freq(source: str, message: str) -> None:
    with pytest.raises(SASParseError, match=message):
        parse_sas(source)


# --- PROC SQL ------------------------------------------------------------------


def query_shape(source: str) -> object:
    """The shape of the query of a PROC SQL CREATE TABLE statement."""
    program = parse_sas(f"proc sql; create table out as {source}; quit;")
    assert program.issues == ()
    [step] = program.steps
    [statement] = step.statements
    return shape(statement.query)


def test_a_proc_sql_query() -> None:
    assert query_shape(
        "select distinct c.name, sum(o.amount) as total format=dollar8. "
        "from sales.customers(keep=id name) as c "
        "inner join orders o on c.id = o.customer_id "
        "left outer join notes on c.id = notes.id "
        "where o.status = 'completed' "
        "group by c.name having count(*) > 1 order by 2 desc, name"
    ) == (
        "SqlQuery",
        True,
        (
            ("SqlItem", ("Qualified", "c", "name"), None),
            (
                "SqlItem",
                ("Call", "sum", (("Qualified", "o", "amount"),), False, False),
                "total",
            ),
        ),
        (
            "SqlTable",
            ("Dataset", "sales", "customers", (("KeepOption", ("id", "name")),)),
            "c",
        ),
        (
            (
                "SqlJoin",
                "inner",
                ("SqlTable", ("Dataset", None, "orders", ()), "o"),
                binary(
                    "=", ("Qualified", "c", "id"), ("Qualified", "o", "customer_id")
                ),
            ),
            (
                "SqlJoin",
                "left",
                ("SqlTable", ("Dataset", None, "notes", ()), None),
                binary("=", ("Qualified", "c", "id"), ("Qualified", "notes", "id")),
            ),
        ),
        binary("=", ("Qualified", "o", "status"), string("completed")),
        (("Qualified", "c", "name"),),
        binary(">", ("Call", "count", (), True, False), number(1)),
        (("SqlOrder", number(2), True), ("SqlOrder", name("name"), False)),
    )


def test_select_star_and_count_distinct() -> None:
    assert query_shape("select *, count(distinct id) as n from t join u on a = b")[
        2
    ] == (
        ("SqlItem", None, None),
        ("SqlItem", ("Call", "count", (name("id"),), False, True), "n"),
    )


def test_proc_sql_options_and_statements() -> None:
    program = parse_sas(
        "proc sql noprint outobs=10; select * from t; "
        "create table a as select x from t; insert into a values (1); "
        "create table b like a; quit;"
    )

    assert issues(program) == [
        "PROC SQL option OUTOBS=10",
        "SELECT statement in PROC SQL",
        "INSERT statement in PROC SQL",
        "CREATE TABLE without AS SELECT",
    ]
    assert len(program.steps[0].statements) == 1


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("select a from t right join u on a = b", "RIGHT join"),
        ("select a from t, u", ", join"),
        ("select a from t union select b from u", "UNION in a query"),
        ("select a length=8 from t", "column modifier LENGTH="),
        ("select a from t where a in (select b from u)", "subquery"),
        ("select (select max(b) from u) as m from t", "subquery"),
    ],
)
def test_unsupported_proc_sql_constructs(query: str, message: str) -> None:
    program = parse_sas(f"proc sql; create table out as {query}; quit;")

    assert issues(program) == [message]


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("from t", "Expected SELECT"),
        ("select a", "Expected FROM, found 'end of statement'"),
        ("select a b from t", "Expected FROM, found 'b'"),
        ("select a from t join u", "Expected ON after the joined table"),
        ("select a from t left u on a = b", "Expected JOIN"),
        ("select a from t group a", "Expected BY"),
        ("select a as from t", "Expected a column name after AS"),
        ("select a from t as where a = 1", "Expected a table alias after AS"),
        ("select a from t where a in (select b from u", "Expected \\) to end"),
    ],
)
def test_invalid_proc_sql(query: str, message: str) -> None:
    with pytest.raises(SASParseError, match=message):
        parse_sas(f"proc sql; create table out as {query}; quit;")


def test_a_qualified_name_needs_no_spaces() -> None:
    assert where("a.b = 1")[0] == binary("=", ("Qualified", "a", "b"), number(1))


# --- FORMAT, data lines, and PROC PRINT ----------------------------------------------


@pytest.mark.parametrize(
    ("statement", "names", "text_names"),
    [
        ("format d date9.", ("d",), set()),
        (
            "format a b date9. c $20. e 8.2 f comma10.2 g",
            ("a", "b", "c", "e", "f", "g"),
            {"c"},
        ),
        ("format x best12. y $char20. z yymmdd10.", ("x", "y", "z"), {"y"}),
        ("format d", ("d",), set()),  # removes d's format
        ("FORMAT D DATE.", ("D",), set()),
        ("format A b $10. c", ("A", "b", "c"), {"a", "b"}),
    ],
)
def test_a_format_statement_names_its_variables(
    statement: str, names: tuple, text_names: set[str]
) -> None:
    program = parse_sas(f"data a; set b; {statement}; run;")

    assert program.issues == ()
    assert program.steps[0].statements[1] == FormatStatement(
        names, program.steps[0].statements[1].span, frozenset(text_names)
    )


def test_a_variable_list_in_a_format_statement_is_reported() -> None:
    program = parse_sas("data a; set b; format x1-x3 8.2; run;")

    assert issues(program) == ["variable list in a FORMAT statement"]


def test_data_lines_are_data_not_statements() -> None:
    program = parse_sas(
        "data a;\n  input x y;\n  datalines;\n101 John USA\n102 Sarah UK\n;\nrun;"
    )

    statement = program.steps[0].statements[-1]
    assert isinstance(statement, DatalinesStatement)
    assert statement.text == "101 John USA\n102 Sarah UK\n"
    assert statement.line == 4
    # The rows are not reported as statements (101 statement, ...).
    assert issues(program) == []


def inline(statement: str) -> tuple[object, list[str]]:
    """The shape of the first statement of a DATA step with data lines, and
    the issues found."""
    program = parse_sas(f"data a; {statement}; datalines;\n1\n;\nrun;")
    return shape(program.steps[0].statements[0]), issues(program)


def field(name: str, text: bool = False, informat: str | None = None) -> tuple:
    return ("InputField", name, text, informat)


def test_input_with_list_input() -> None:
    assert inline("input id name $ when :date9. note :$20. Amount :8.2 n:best12.") == (
        (
            "InputStatement",
            (
                field("id"),
                field("name", True),
                field("when", informat="DATE9."),
                field("note", informat="$20."),
                field("Amount", informat="8.2"),
                field("n", informat="BEST12."),
            ),
        ),
        [],
    )


def test_input_keeps_names_without_a_blank_before_the_dollar() -> None:
    assert inline("INPUT name$ city $")[0] == (
        "InputStatement",
        (field("name", True), field("city", True)),
    )


@pytest.mark.parametrize(
    ("statement", "message"),
    [
        ("input id 1-3", "column or formatted input of id"),
        ("input name $ 1-10", "column or formatted input of name"),
        ("input when date9.", "column or formatted input of when"),
        ("input amount 8.2", "column or formatted input of amount"),
        ("input @3 id", "@ in an INPUT statement"),
        ("input id @@", "@ in an INPUT statement"),
        ("input id / name", "/ in an INPUT statement"),
        ("input id #2 name", "# in an INPUT statement"),
        ("input id +1 name", "+ in an INPUT statement"),
        ("input (a b) ($)", "( in an INPUT statement"),
        ("input x1-x3", "variable list in an INPUT statement"),
    ],
)
def test_unsupported_input(statement: str, message: str) -> None:
    assert inline(statement)[1] == [message]


def test_input_needs_variables() -> None:
    with pytest.raises(SASParseError, match="INPUT without variables"):
        parse_sas("data a; input; datalines;\n1\n;\nrun;")


def test_data_lines_outside_a_data_step_are_one_issue() -> None:
    program = parse_sas("datalines;\n101 John\n;\ndata a; set b; run;")

    assert issues(program) == ["DATALINES statement"]


def test_proc_print() -> None:
    program = parse_sas(
        "proc print data=sales.a(keep=x y) noobs label obs='Row' width=full; "
        "var y x; var z; where x > 1; run;"
    )

    assert program.issues == ()
    assert shape(program.steps) == (
        (
            "ProcPrint",
            ("Dataset", "sales", "a", (("KeepOption", ("x", "y")),)),
            ("y", "x", "z"),
            (("WhereStatement", binary(">", name("x"), number(1))),),
        ),
    )


def test_proc_print_without_data() -> None:
    program = parse_sas("proc print; run;")

    assert shape(program.steps) == (("ProcPrint", None, (), ()),)


def test_proc_print_options_and_statements_that_change_rows_are_reported() -> None:
    program = parse_sas(
        "proc print data=a sumlabel style=minimal; id x; by y; sum z; run;"
    )

    assert issues(program) == [
        "PROC PRINT option STYLE=",
        "ID statement in PROC PRINT",
        "BY statement in PROC PRINT",
        "SUM statement in PROC PRINT",
    ]


# --- CASE and CALCULATED ---------------------------------------------------------


def select_item(source: str) -> object:
    """The shape of the first SELECT item of a PROC SQL query."""
    return query_shape(f"select {source} as v from t")[2][0][1]


def test_case_with_conditions() -> None:
    assert select_item("case when x > 1 then 'a' when x > 0 then 'b' else 'c' end") == (
        "CaseExpression",
        None,
        (binary(">", name("x"), number(1)), binary(">", name("x"), number(0))),
        (("String", "a"), ("String", "b")),
        ("String", "c"),
    )


def test_case_with_an_operand_and_without_else() -> None:
    assert select_item("case t.kind when 'A' then 1 when 'B' then 2 end") == (
        "CaseExpression",
        ("Qualified", "t", "kind"),
        (("String", "A"), ("String", "B")),
        (number(1), number(2)),
        None,
    )


def test_case_inside_a_function_and_inside_case() -> None:
    inner = ("CaseExpression", None, (name("y"),), (number(1),), number(0))
    assert select_item("sum(case when x then case when y then 1 else 0 end end)") == (
        "Call",
        "sum",
        (("CaseExpression", None, (name("x"),), (inner,), None),),
        False,
        False,
    )


def test_a_variable_named_case() -> None:
    assert statements("if case then y = case + 1;") == (
        (
            "IfThen",
            name("case"),
            assign("y", binary("+", name("case"), number(1))),
            None,
        ),
    )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("case when x then 1 as v from t", "Expected END to end the CASE"),
        ("case when x 1 end as v from t", "Expected THEN"),
    ],
)
def test_invalid_case(source: str, message: str) -> None:
    with pytest.raises(SASParseError, match=message):
        parse_sas(f"proc sql; create table out as select {source}; quit;")


def test_calculated() -> None:
    assert select_item("calculated total * 2") == (
        "Binary",
        "*",
        ("Calculated", "total"),
        number(2),
    )
