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
        "proc transpose data=a out=b; by x; run; "
        "proc sql; select 1; quit; "
        "data c; set b; run;"
    )

    assert issues(program) == ["PROC TRANSPOSE step", "PROC SQL step"]
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
