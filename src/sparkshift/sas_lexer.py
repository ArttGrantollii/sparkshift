"""Split a SAS program into tokens.

SAS is not SQL, so it has its own front end: this lexer, the parser in
sas_parser.py, and the translator in sas_translate.py, which produces the
same IR the SQL front end does (see ADR 0005).

The lexer knows SAS's comment forms: ``/* ... */`` anywhere, and a statement
that starts with ``*``, which runs to the next semicolon. Names are not
case-sensitive in SAS; tokens keep their spelling, and the parser compares
keywords ignoring case. Macro syntax (``&name``, ``%let``) becomes MACRO
tokens: the macro language runs before the program and is not supported.
"""

import re
from dataclasses import dataclass
from enum import Enum
from typing import NoReturn

from sparkshift.errors import SASParseError


class Kind(Enum):
    NAME = "name"
    NUMBER = "number"
    STRING = "string"
    SYMBOL = "symbol"
    MACRO = "macro"
    END = "end"


@dataclass(frozen=True)
class Token:
    kind: Kind
    text: str
    # Where the token starts and ends in the source, and its 1-based line and
    # column, for messages.
    start: int
    end: int
    line: int
    column: int
    # A string literal's value, and the letters right after its closing
    # quote, such as d in '01jan2024'd.
    value: str = ""
    suffix: str = ""

    def is_keyword(self, *words: str) -> bool:
        return self.kind is Kind.NAME and self.text.upper() in words

    def is_symbol(self, *symbols: str) -> bool:
        return self.kind is Kind.SYMBOL and self.text in symbols


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER = re.compile(r"(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
# Longest first, so "**" is not read as two "*".
_SYMBOLS = (
    "**", "||", "!!", "^=", "~=", "¬=", "<=", ">=", "=<", "=>", "<>", "><",
    "(", ")", ",", ";", "=", "<", ">", "+", "-", "*", "/", "&", "|", "!",
    "^", "~", "¬", "¦", ":", ".", "$", "@", "#",
)  # fmt: skip


def tokenize(source: str) -> list[Token]:
    """Return the program's tokens, ending with an END token."""
    return _Lexer(source).tokens()


class _Lexer:
    def __init__(self, source: str) -> None:
        self.source = source
        self.position = 0
        # Whether the next token starts a statement, where "*" starts a
        # comment rather than meaning multiplication.
        self.statement_start = True

    def tokens(self) -> list[Token]:
        found = []
        while True:
            self.skip_blanks_and_comments()
            if self.position >= len(self.source):
                found.append(self.token(Kind.END, self.position))
                return found
            token = self.next_token()
            self.statement_start = token.is_symbol(";")
            found.append(token)

    def skip_blanks_and_comments(self) -> None:
        source = self.source
        while self.position < len(source):
            if source[self.position].isspace():
                self.position += 1
            elif source.startswith("/*", self.position):
                end = source.find("*/", self.position + 2)
                if end < 0:
                    self.fail("Unclosed comment: /* without */", self.position)
                self.position = end + 2
            elif self.statement_start and source[self.position] == "*":
                # A comment statement: * text ;
                end = source.find(";", self.position)
                if end < 0:
                    self.fail(
                        "Comment statement without a closing semicolon", self.position
                    )
                self.position = end + 1
            else:
                return

    def next_token(self) -> Token:
        source, start = self.source, self.position
        char = source[start]
        if char in "'\"":
            return self.string(start)
        if char in "&%" and _NAME.match(source, start + 1):
            # &name resolves a macro variable; %name calls a macro statement.
            match = _NAME.match(source, start + 1)
            assert match is not None
            self.position = match.end()
            return self.token(Kind.MACRO, start)
        name = _NAME.match(source, start)
        if name:
            self.position = name.end()
            return self.token(Kind.NAME, start)
        number = _NUMBER.match(source, start)
        if number:
            self.position = number.end()
            return self.token(Kind.NUMBER, start)
        for symbol in _SYMBOLS:
            if source.startswith(symbol, start):
                self.position = start + len(symbol)
                return self.token(Kind.SYMBOL, start)
        self.fail(f"Unexpected character {char!r}", start)

    def string(self, start: int) -> Token:
        """A quoted string; a doubled quote stands for the quote itself."""
        source, quote = self.source, self.source[start]
        position, value = start + 1, []
        while True:
            end = source.find(quote, position)
            if end < 0:
                self.fail("Unclosed string", start)
            value.append(source[position:end])
            if source.startswith(quote * 2, end):
                value.append(quote)
                position = end + 2
                continue
            position = end + 1
            break
        suffix = _NAME.match(source, position)
        self.position = suffix.end() if suffix else position
        return self.token(
            Kind.STRING,
            start,
            value="".join(value),
            suffix=suffix.group() if suffix else "",
        )

    def token(self, kind: Kind, start: int, value: str = "", suffix: str = "") -> Token:
        line, column = self.line_and_column(start)
        end = max(self.position, start)
        text = self.source[start:end]
        return Token(kind, text, start, end, line, column, value, suffix)

    def line_and_column(self, position: int) -> tuple[int, int]:
        line = self.source.count("\n", 0, position) + 1
        column = position - (self.source.rfind("\n", 0, position) + 1) + 1
        return line, column

    def fail(self, message: str, position: int) -> NoReturn:
        line, column = self.line_and_column(position)
        raise SASParseError(message, line=line, column=column)
