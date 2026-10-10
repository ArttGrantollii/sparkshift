"""Parse a SAS program into a small syntax tree.

The tree covers what SparkShift translates; anything else that is valid SAS
is recorded as an issue (an unsupported construct), so every problem can be
reported at once. Input that is not valid SAS raises SASParseError.

A program is a sequence of steps. A DATA step runs from its DATA statement to
RUN, or to the next DATA or PROC statement, as in SAS. Keywords are not
case-sensitive.
"""

import dataclasses
from dataclasses import dataclass, field

from sparkshift.diagnostics import Diagnostic
from sparkshift.errors import SASParseError
from sparkshift.sas_lexer import Kind, Token, tokenize

# --- Syntax tree --------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """Where a construct is in the source, for messages."""

    start: int
    end: int
    line: int
    column: int


# Expressions


@dataclass(frozen=True)
class Name:
    name: str
    span: Span


@dataclass(frozen=True)
class Number:
    value: int | float
    span: Span


@dataclass(frozen=True)
class String:
    value: str
    span: Span


@dataclass(frozen=True)
class Missing:
    """The missing numeric value, written as a period."""

    span: Span


@dataclass(frozen=True)
class Binary:
    # A comparison ("=", "^=", "<", "<=", ">", ">="), "AND", "OR", or an
    # arithmetic or concatenation operator ("+", "-", "*", "/", "**", "||").
    op: str
    left: "Expression"
    right: "Expression"
    span: Span


@dataclass(frozen=True)
class Unary:
    op: str  # "NOT", "-", or "+"
    operand: "Expression"
    span: Span


@dataclass(frozen=True)
class In:
    expression: "Expression"
    values: tuple["Expression", ...]
    negated: bool
    span: Span


@dataclass(frozen=True)
class Between:
    expression: "Expression"
    low: "Expression"
    high: "Expression"
    negated: bool
    span: Span


@dataclass(frozen=True)
class IsMissing:
    expression: "Expression"
    negated: bool
    span: Span


@dataclass(frozen=True)
class Call:
    name: str
    arguments: tuple["Expression", ...]
    span: Span


Expression = (
    Name | Number | String | Missing | Binary | Unary | In | Between | IsMissing | Call
)


# Data sets and their options


@dataclass(frozen=True)
class KeepOption:
    names: tuple[str, ...]
    span: Span


@dataclass(frozen=True)
class DropOption:
    names: tuple[str, ...]
    span: Span


@dataclass(frozen=True)
class RenameOption:
    pairs: tuple[tuple[str, str], ...]
    span: Span


@dataclass(frozen=True)
class WhereOption:
    condition: Expression
    span: Span


DatasetOption = KeepOption | DropOption | RenameOption | WhereOption


@dataclass(frozen=True)
class Dataset:
    library: str | None
    member: str
    options: tuple[DatasetOption, ...]
    span: Span


# Statements and steps


@dataclass(frozen=True)
class SetStatement:
    datasets: tuple[Dataset, ...]
    span: Span


@dataclass(frozen=True)
class WhereStatement:
    condition: Expression
    span: Span


@dataclass(frozen=True)
class KeepStatement:
    names: tuple[str, ...]
    span: Span


@dataclass(frozen=True)
class DropStatement:
    names: tuple[str, ...]
    span: Span


@dataclass(frozen=True)
class RenameStatement:
    pairs: tuple[tuple[str, str], ...]
    span: Span


Statement = (
    SetStatement | WhereStatement | KeepStatement | DropStatement | RenameStatement
)


@dataclass(frozen=True)
class DataStep:
    outputs: tuple[Dataset, ...]
    statements: tuple[Statement, ...]
    span: Span


@dataclass(frozen=True)
class Program:
    steps: tuple[DataStep, ...]
    # Valid SAS that SparkShift does not translate.
    issues: tuple[Diagnostic, ...] = field(default=())


def parse_sas(source: str) -> Program:
    """Parse a SAS program.

    Raises:
        SASParseError: the program is empty or is not valid SAS.
    """
    return _Parser(source).program()


# --- Parser -------------------------------------------------------------------

_COMPARISONS = {
    "=": "=",
    "EQ": "=",
    "^=": "^=",
    "~=": "^=",
    "¬=": "^=",
    "NE": "^=",
    "<": "<",
    "LT": "<",
    "<=": "<=",
    "=<": "<=",
    "LE": "<=",
    ">": ">",
    "GT": ">",
    ">=": ">=",
    "=>": ">=",
    "GE": ">=",
}
_NOT_SYMBOLS = ("^", "~", "¬")
_OR_SYMBOLS = ("|", "!", "¦")
_STEP_KEYWORDS = ("DATA", "PROC")
_STEP_ENDS = ("RUN", "QUIT")
# Statements valid in a DATA step that SparkShift does not translate yet; any
# other statement there is reported by its first word.
_ASSIGNMENT_HINT = "Assignments are not supported yet."


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.issues: list[Diagnostic] = []
        self.statements = self.split(tokenize(source))
        self.index = 0

    # --- Statements ----------------------------------------------------------

    def split(self, tokens: list[Token]) -> list[list[Token]]:
        """The program's statements, each a list of tokens without its
        semicolon. An empty statement (a lone semicolon) is skipped."""
        statements: list[list[Token]] = []
        current: list[Token] = []
        for token in tokens[:-1]:  # the last token is END
            if token.is_symbol(";"):
                if current:
                    statements.append(current)
                current = []
            else:
                current.append(token)
        if current:
            raise self.error("Statement without a closing semicolon", current[0])
        return statements

    def program(self) -> Program:
        steps = []
        while self.index < len(self.statements):
            statement = self.statements[self.index]
            first = statement[0]
            if first.is_keyword("DATA"):
                steps.append(self.data_step())
            elif first.is_keyword("PROC"):
                self.proc_step()
            elif first.is_keyword(*_STEP_ENDS):
                self.index += 1  # a RUN with no step to end does nothing
            else:
                self.unsupported(self.statement_name(statement), statement)
                self.index += 1
        if not steps and not self.issues:
            raise SASParseError("No DATA or PROC step found")
        return Program(tuple(steps), tuple(self.issues))

    def data_step(self) -> DataStep:
        header = self.statements[self.index]
        self.index += 1
        outputs = self.datasets(_Tokens(header[1:], self), "DATA")
        statements: list[Statement] = []
        last = header
        while self.index < len(self.statements):
            statement = self.statements[self.index]
            first = statement[0]
            if first.is_keyword(*_STEP_KEYWORDS):
                break  # the next step starts; SAS ends this one
            self.index += 1
            last = statement
            if first.is_keyword(*_STEP_ENDS):
                break
            parsed = self.data_statement(statement)
            if parsed is not None:
                statements.append(parsed)
        return DataStep(outputs, tuple(statements), self.span(header[0], last[-1]))

    def proc_step(self) -> None:
        header = self.statements[self.index]
        if len(header) < 2:
            raise self.error("PROC without a procedure name", header[0])
        self.unsupported(f"PROC {header[1].text.upper()} step", header)
        self.index += 1
        while self.index < len(self.statements):
            first = self.statements[self.index][0]
            if first.is_keyword(*_STEP_KEYWORDS):
                return
            self.index += 1
            if first.is_keyword(*_STEP_ENDS):
                return

    def data_statement(self, statement: list[Token]) -> Statement | None:
        first = statement[0]
        tokens = _Tokens(statement[1:], self)
        span = self.span(statement[0], statement[-1])
        if first.is_keyword("SET"):
            return SetStatement(self.datasets(tokens, "SET"), span)
        if first.is_keyword("WHERE"):
            condition = self.expression(tokens)
            tokens.expect_end()
            return WhereStatement(condition, span)
        if first.is_keyword("KEEP"):
            return KeepStatement(self.names(tokens, "KEEP"), span)
        if first.is_keyword("DROP"):
            return DropStatement(self.names(tokens, "DROP"), span)
        if first.is_keyword("RENAME"):
            pairs = self.renames(tokens)
            tokens.expect_end()
            return RenameStatement(pairs, span)
        if (
            first.kind is Kind.NAME
            and len(statement) > 1
            and statement[1].is_symbol("=")
        ):
            self.unsupported("assignment statement", statement, _ASSIGNMENT_HINT)
            return None
        self.unsupported(self.statement_name(statement), statement)
        return None

    def statement_name(self, statement: list[Token]) -> str:
        first = statement[0]
        if first.kind is Kind.MACRO:
            return f"macro {first.text}"
        return f"{first.text.upper()} statement"

    # --- Data sets -----------------------------------------------------------

    def datasets(self, tokens: "_Tokens", statement: str) -> tuple[Dataset, ...]:
        """Data set names with their options, then any statement options
        (NAME=value), which are reported."""
        found = []
        while not tokens.at_end() and not tokens.at_option():
            found.append(self.dataset(tokens))
        while not tokens.at_end():
            option = tokens.peek()
            if tokens.at_option():
                self.unsupported(f"{statement} option {option.text.upper()}=", [option])
                tokens.skip_option()
            else:
                raise self.error(
                    f"Unexpected {option.text!r} in the {statement} statement", option
                )
        return tuple(found)

    def dataset(self, tokens: "_Tokens") -> Dataset:
        first = tokens.take_name("a data set name")
        library, member, last = None, first.text, first
        if tokens.peek().is_symbol(".") and tokens.adjacent(first):
            tokens.take()
            last = tokens.take_name("a data set name after the period")
            library, member = first.text, last.text
        options: list[DatasetOption] = []
        if tokens.peek().is_symbol("("):
            tokens.take()
            while not tokens.peek().is_symbol(")"):
                option = self.dataset_option(tokens)
                if option is not None:
                    options.append(option)
            last = tokens.take()
        return Dataset(library, member, tuple(options), self.span(first, last))

    def dataset_option(self, tokens: "_Tokens") -> DatasetOption | None:
        name = tokens.take_name("a data set option")
        tokens.expect_symbol("=")
        keyword = name.text.upper()
        if keyword in ("KEEP", "DROP"):
            names = self.names(tokens, f"{keyword}=", stop_at_option=True)
            span = self.span(name, tokens.previous())
            return (
                KeepOption(names, span)
                if keyword == "KEEP"
                else DropOption(names, span)
            )
        if keyword in ("RENAME", "WHERE"):
            tokens.expect_symbol("(")
            if keyword == "RENAME":
                pairs = self.renames(tokens, stop=")")
                closing = tokens.expect_symbol(")")
                return RenameOption(pairs, self.span(name, closing))
            condition = self.expression(tokens)
            closing = tokens.expect_symbol(")")
            return WhereOption(condition, self.span(name, closing))
        self.unsupported(f"data set option {keyword}=", [name])
        tokens.skip_option_value()
        return None

    def names(
        self, tokens: "_Tokens", where: str, *, stop_at_option: bool = False
    ) -> tuple[str, ...]:
        """Variable names, as in KEEP a b c. Variable lists (x1-x5, a--c,
        prefix:, _ALL_) are reported."""
        names = []
        lists = False
        while not tokens.at_end() and not tokens.peek().is_symbol(")"):
            if stop_at_option and tokens.at_option():
                break
            token = tokens.take()
            following = tokens.peek()
            if token.kind is not Kind.NAME or token.text.upper() in _SPECIAL_LISTS:
                self.unsupported(f"variable list in {where}", [token])
                lists = True
            elif following.is_symbol("-", ":"):
                # x1-x5, a--c, or prefix: (there is no arithmetic in a list).
                tokens.take()
                last = following
                if following.is_symbol("-"):
                    if tokens.peek().is_symbol("-"):
                        tokens.take()
                    last = tokens.take()
                self.unsupported(f"variable list in {where}", [token, last])
                lists = True
            else:
                names.append(token.text)
        if not names and not lists:
            raise self.error(f"{where} names no variables", tokens.previous())
        return tuple(names)

    def renames(self, tokens: "_Tokens", stop: str = "") -> tuple[tuple[str, str], ...]:
        pairs = []
        while not tokens.at_end() and not (stop and tokens.peek().is_symbol(stop)):
            old = tokens.take_name("a variable to rename")
            tokens.expect_symbol("=")
            new = tokens.take_name("the variable's new name")
            pairs.append((old.text, new.text))
        if not pairs:
            raise self.error("RENAME names no variables", tokens.previous())
        return tuple(pairs)

    # --- Expressions ---------------------------------------------------------

    def expression(self, tokens: "_Tokens") -> Expression:
        return self.or_(tokens)

    def or_(self, tokens: "_Tokens") -> Expression:
        left = self.and_(tokens)
        while tokens.peek().is_keyword("OR") or tokens.peek().is_symbol(*_OR_SYMBOLS):
            tokens.take()
            right = self.and_(tokens)
            left = Binary("OR", left, right, self.join(left, right))
        return left

    def and_(self, tokens: "_Tokens") -> Expression:
        left = self.comparison(tokens)
        while tokens.peek().is_keyword("AND") or tokens.peek().is_symbol("&"):
            tokens.take()
            right = self.comparison(tokens)
            left = Binary("AND", left, right, self.join(left, right))
        return left

    def comparison(self, tokens: "_Tokens") -> Expression:
        left = self.concatenation(tokens)
        negated = False
        if tokens.peek().is_keyword("NOT") and tokens.peek(1).is_keyword(
            "IN", "BETWEEN"
        ):
            tokens.take()
            negated = True
        token = tokens.peek()
        if token.is_keyword("IN"):
            return self.in_list(left, tokens, negated)
        if token.is_keyword("BETWEEN"):
            tokens.take()
            low = self.concatenation(tokens)
            if not tokens.peek().is_keyword("AND"):
                raise self.error("BETWEEN without AND", tokens.peek())
            tokens.take()
            high = self.concatenation(tokens)
            return Between(left, low, high, negated, self.join(left, high))
        if token.is_keyword("IS"):
            tokens.take()
            if tokens.peek().is_keyword("NOT"):
                tokens.take()
                negated = True
            missing = tokens.take()
            if not missing.is_keyword("MISSING", "NULL"):
                raise self.error("IS must be followed by MISSING or NULL", missing)
            return IsMissing(left, negated, self.span_of(left, missing))
        op = self.comparison_operator(tokens)
        if op is None:
            return left
        right = self.concatenation(tokens)
        expression = Binary(op, left, right, self.join(left, right))
        if self.comparison_operator(tokens, consume=False) is not None:
            tokens.take()
            last = self.concatenation(tokens)
            self.unsupported(
                "chained comparison",
                self.join(expression, last),
                "Write each comparison separately, joined with AND.",
            )
        return expression

    def comparison_operator(
        self, tokens: "_Tokens", *, consume: bool = True
    ) -> str | None:
        token = tokens.peek()
        if token.is_symbol("<>", "><"):
            self.unsupported(
                f"the {token.text} operator",
                [token],
                "In SAS it means MIN or MAX, or not equal in WHERE; "
                "write the comparison you mean.",
            )
            tokens.take()
            return "^="
        key = token.text.upper() if token.kind is Kind.NAME else token.text
        if token.kind not in (Kind.NAME, Kind.SYMBOL) or key not in _COMPARISONS:
            return None
        if consume:
            tokens.take()
            if tokens.peek().is_symbol(":") and tokens.adjacent(token):
                self.unsupported(
                    "comparison with a colon modifier", [token, tokens.take()]
                )
        return _COMPARISONS[key]

    def in_list(self, left: Expression, tokens: "_Tokens", negated: bool) -> Expression:
        tokens.take()
        tokens.expect_symbol("(")
        values: list[Expression] = []
        ranges = False
        while not tokens.peek().is_symbol(")"):
            if tokens.peek().is_symbol(","):
                tokens.take()
                continue
            value = self.unary(tokens)
            if tokens.peek().is_symbol(":"):
                tokens.take()
                end = self.unary(tokens)
                self.unsupported(
                    "range in an IN list", self.join(value, end), "List the values."
                )
                ranges = True
                continue
            values.append(value)
        closing = tokens.take()
        if not values and not ranges:
            raise self.error("IN with an empty list", closing)
        return In(left, tuple(values), negated, self.span_of(left, closing))

    def concatenation(self, tokens: "_Tokens") -> Expression:
        left = self.additive(tokens)
        while tokens.peek().is_symbol("||", "!!"):
            tokens.take()
            right = self.additive(tokens)
            left = Binary("||", left, right, self.join(left, right))
        return left

    def additive(self, tokens: "_Tokens") -> Expression:
        left = self.multiplicative(tokens)
        while tokens.peek().is_symbol("+", "-"):
            op = tokens.take().text
            right = self.multiplicative(tokens)
            left = Binary(op, left, right, self.join(left, right))
        return left

    def multiplicative(self, tokens: "_Tokens") -> Expression:
        left = self.unary(tokens)
        while tokens.peek().is_symbol("*", "/"):
            op = tokens.take().text
            right = self.unary(tokens)
            left = Binary(op, left, right, self.join(left, right))
        return left

    def unary(self, tokens: "_Tokens") -> Expression:
        token = tokens.peek()
        if token.is_symbol("-", "+"):
            tokens.take()
            operand = self.unary(tokens)
            return Unary(token.text, operand, self.span_of(token, operand))
        if token.is_keyword("NOT") or token.is_symbol(*_NOT_SYMBOLS):
            tokens.take()
            if tokens.peek().is_symbol("("):
                operand = self.unary(tokens)
                return Unary("NOT", operand, self.span_of(token, operand))
            # In SAS, NOT binds more tightly than a comparison: "not a = b"
            # means "(not a) = b". Parentheses make the meaning explicit. The
            # rest of the comparison is read too, so it is reported once.
            operand = self.comparison(tokens)
            span = self.span_of(token, operand)
            self.unsupported("NOT without parentheses", span, "Write NOT (condition).")
            return Unary("NOT", operand, span)
        return self.power(tokens)

    def power(self, tokens: "_Tokens") -> Expression:
        base = self.primary(tokens)
        if tokens.peek().is_symbol("**"):
            tokens.take()
            exponent = self.unary(tokens)  # right to left, as in SAS
            return Binary("**", base, exponent, self.join(base, exponent))
        return base

    def primary(self, tokens: "_Tokens") -> Expression:
        token = tokens.take()
        span = self.span(token, token)
        if token.kind is Kind.NUMBER:
            text = token.text
            is_integer = text.isdigit()
            return Number(int(text) if is_integer else float(text), span)
        if token.kind is Kind.STRING:
            if token.suffix:
                self.unsupported(
                    f"{token.suffix.upper()} literal",
                    [token],
                    "Date, time, and other suffixed constants are not supported yet.",
                )
            elif token.text.startswith('"') and (
                "&" in token.value or "%" in token.value
            ):
                self.unsupported("macro reference in a string", [token])
            return String(token.value, span)
        if token.is_symbol("."):
            following = tokens.peek()
            if following.kind is Kind.NAME and tokens.adjacent(token):
                tokens.take()
                self.unsupported(
                    "special missing value",
                    [token, following],
                    "Only the ordinary missing value, a period, is supported.",
                )
            return Missing(span)
        if token.kind is Kind.MACRO:
            self.unsupported(f"macro {token.text}", [token])
            return Name(token.text, span)
        if token.kind is Kind.NAME:
            if tokens.peek().is_symbol("("):
                return self.call(token, tokens)
            return Name(token.text, span)
        if token.is_symbol("("):
            inner = self.expression(tokens)
            closing = tokens.expect_symbol(")")
            return _with_span(inner, self.span(token, closing))
        raise self.error(
            f"Unexpected {token.text or 'end of statement'!r} in an expression", token
        )

    def call(self, name: Token, tokens: "_Tokens") -> Expression:
        tokens.take()
        arguments: list[Expression] = []
        while not tokens.peek().is_symbol(")"):
            arguments.append(self.expression(tokens))
            if tokens.peek().is_symbol(","):
                tokens.take()
            elif not tokens.peek().is_symbol(")"):
                raise self.error("Expected , or ) in a function call", tokens.peek())
        closing = tokens.take()
        return Call(name.text, tuple(arguments), self.span(name, closing))

    # --- Helpers -------------------------------------------------------------

    def span(self, first: Token, last: Token) -> Span:
        return Span(first.start, last.end, first.line, first.column)

    def join(self, left: Expression, right: Expression) -> Span:
        return Span(left.span.start, right.span.end, left.span.line, left.span.column)

    def span_of(self, first: Token | Expression, last: Token | Expression) -> Span:
        start = first.span if not isinstance(first, Token) else self.span(first, first)
        end = last.span if not isinstance(last, Token) else self.span(last, last)
        return Span(start.start, end.end, start.line, start.column)

    def unsupported(
        self, message: str, where: list[Token] | Span, hint: str | None = None
    ) -> None:
        """Record valid SAS that SparkShift does not translate, located by its
        tokens or span."""
        if isinstance(where, Span):
            start, end = where.start, where.end
        else:
            start, end = where[0].start, where[-1].end
        fragment = " ".join(self.source[start:end].split())
        self.issues.append(Diagnostic(message, fragment, hint))

    def error(self, message: str, token: Token) -> SASParseError:
        return SASParseError(message, line=token.line, column=token.column)


_SPECIAL_LISTS = frozenset({"_ALL_", "_NUMERIC_", "_CHARACTER_"})


def _with_span(expression: Expression, span: Span) -> Expression:
    """The expression with the span of its parentheses included."""
    return dataclasses.replace(expression, span=span)


class _Tokens:
    """The tokens of one statement, read one at a time."""

    def __init__(self, tokens: list[Token], parser: _Parser) -> None:
        self.tokens = tokens
        self.index = 0
        self.parser = parser
        end = tokens[-1].end if tokens else 0
        line = tokens[-1].line if tokens else 1
        column = tokens[-1].column if tokens else 1
        self.sentinel = Token(Kind.END, "", end, end, line, column)

    def peek(self, offset: int = 0) -> Token:
        index = self.index + offset
        return self.tokens[index] if index < len(self.tokens) else self.sentinel

    def take(self) -> Token:
        token = self.peek()
        self.index += 1
        return token

    def previous(self) -> Token:
        return self.tokens[self.index - 1] if self.index > 0 else self.sentinel

    def at_end(self) -> bool:
        return self.index >= len(self.tokens)

    def at_option(self) -> bool:
        """Whether the next tokens are NAME =, which starts an option."""
        return self.peek().kind is Kind.NAME and self.peek(1).is_symbol("=")

    def adjacent(self, token: Token) -> bool:
        """Whether the next token directly follows ``token``, with no space,
        as the period in lib.member does."""
        return self.peek().start == token.end

    def take_name(self, what: str) -> Token:
        token = self.take()
        if token.kind is not Kind.NAME:
            raise self.parser.error(f"Expected {what}", token)
        return token

    def expect_symbol(self, symbol: str) -> Token:
        token = self.take()
        if not token.is_symbol(symbol):
            found = token.text or "end of statement"
            raise self.parser.error(f"Expected {symbol!r}, found {found!r}", token)
        return token

    def expect_end(self) -> None:
        if not self.at_end():
            token = self.peek()
            raise self.parser.error(f"Unexpected {token.text!r}", token)

    def skip_option(self) -> None:
        self.take()
        self.take()
        self.skip_option_value()

    def skip_option_value(self) -> None:
        """Skip an option's value: one token, or a parenthesized group (to
        the end of the statement if it is never closed)."""
        depth = 0
        while not self.at_end():
            token = self.take()
            depth += token.is_symbol("(") - token.is_symbol(")")
            if depth == 0:
                return
