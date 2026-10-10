"""Tests for splitting SAS programs into tokens."""

import pytest

from sparkshift.errors import SASParseError, SQLParseError
from sparkshift.sas_lexer import Kind, Token, tokenize


def kinds_and_texts(source: str) -> list[tuple[Kind, str]]:
    return [(token.kind, token.text) for token in tokenize(source)[:-1]]


def test_a_statement() -> None:
    assert kinds_and_texts("set sales.customers;") == [
        (Kind.NAME, "set"),
        (Kind.NAME, "sales"),
        (Kind.SYMBOL, "."),
        (Kind.NAME, "customers"),
        (Kind.SYMBOL, ";"),
    ]


def test_the_last_token_is_the_end() -> None:
    tokens = tokenize("run;")

    assert tokens[-1].kind is Kind.END
    assert tokenize("")[0].kind is Kind.END


@pytest.mark.parametrize(
    ("source", "symbols"),
    [
        ("a ** b", ["**"]),
        ("a ^= b", ["^="]),
        ("a ~= b", ["~="]),
        ("a <= b >= c", ["<=", ">="]),
        ("a || b !! c", ["||", "!!"]),
        ("a <> b >< c", ["<>", "><"]),
        ("a & b | c", ["&", "|"]),
    ],
)
def test_operators_of_several_characters(source: str, symbols: list[str]) -> None:
    found = [text for kind, text in kinds_and_texts(source) if kind is Kind.SYMBOL]

    assert found == symbols


@pytest.mark.parametrize(
    ("source", "number"),
    [
        ("42", "42"),
        ("3.25", "3.25"),
        (".5", ".5"),
        ("1e3", "1e3"),
        ("2.5E-2", "2.5E-2"),
    ],
)
def test_numbers(source: str, number: str) -> None:
    assert kinds_and_texts(source) == [(Kind.NUMBER, number)]


def test_a_lone_period_is_a_symbol() -> None:
    # The parser reads it as the missing value, or as part of lib.member.
    assert kinds_and_texts("x = .;")[2] == (Kind.SYMBOL, ".")


@pytest.mark.parametrize(
    ("source", "value"),
    [
        ("'US'", "US"),
        ('"US"', "US"),
        ("'it''s'", "it's"),
        ('"say ""hi"""', 'say "hi"'),
        ("' '", " "),
        ("''", ""),
    ],
)
def test_strings(source: str, value: str) -> None:
    [token, _end] = tokenize(source)

    assert (token.kind, token.value, token.suffix) == (Kind.STRING, value, "")


def test_a_string_keeps_its_suffix() -> None:
    [token, _end] = tokenize("'01jan2024'd")

    assert (token.value, token.suffix) == ("01jan2024", "d")


def test_block_comments_are_skipped_anywhere() -> None:
    assert kinds_and_texts("data /* the output */ out; /* done */") == [
        (Kind.NAME, "data"),
        (Kind.NAME, "out"),
        (Kind.SYMBOL, ";"),
    ]


def test_a_statement_that_starts_with_an_asterisk_is_a_comment() -> None:
    source = "* keep only US customers;\n  * another one ;\nwhere a = 2 * b;"

    assert kinds_and_texts(source) == [
        (Kind.NAME, "where"),
        (Kind.NAME, "a"),
        (Kind.SYMBOL, "="),
        (Kind.NUMBER, "2"),
        (Kind.SYMBOL, "*"),
        (Kind.NAME, "b"),
        (Kind.SYMBOL, ";"),
    ]


@pytest.mark.parametrize("source", ["&cutoff", "%let", "%put"])
def test_macro_references_are_macro_tokens(source: str) -> None:
    assert kinds_and_texts(source) == [(Kind.MACRO, source)]


def test_an_ampersand_alone_means_and() -> None:
    assert kinds_and_texts("a & b")[1] == (Kind.SYMBOL, "&")


def test_positions_are_one_based_lines_and_columns() -> None:
    tokens = tokenize("data out;\n  set in;")
    set_token = tokens[3]

    assert (set_token.text, set_token.line, set_token.column) == ("set", 2, 3)
    assert (set_token.start, set_token.end) == (12, 15)


@pytest.mark.parametrize(
    ("source", "message", "line", "column"),
    [
        ("data a; /* never closed", "Unclosed comment: /* without */", 1, 9),
        (
            "data a;\n* no semicolon",
            "Comment statement without a closing semicolon",
            2,
            1,
        ),
        ("where name = 'Bob;", "Unclosed string", 1, 14),
        ("where x = `1`;", "Unexpected character '`'", 1, 11),
    ],
)
def test_lexical_errors_point_at_the_problem(
    source: str, message: str, line: int, column: int
) -> None:
    with pytest.raises(SASParseError) as caught:
        tokenize(source)

    assert (caught.value.message, caught.value.line, caught.value.column) == (
        message,
        line,
        column,
    )
    assert isinstance(caught.value, SQLParseError)


def test_keyword_and_symbol_checks() -> None:
    token = Token(Kind.NAME, "Where", 0, 5, 1, 1)

    assert token.is_keyword("WHERE")
    assert not token.is_keyword("SET")
    assert not token.is_symbol("Where")
