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
    """A function call. In PROC SQL, ``star`` marks COUNT(*) and ``distinct``
    COUNT(DISTINCT x)."""

    name: str
    arguments: tuple["Expression", ...]
    span: Span
    star: bool = False
    distinct: bool = False


@dataclass(frozen=True)
class Qualified:
    """table.column, in PROC SQL."""

    qualifier: str
    name: str
    span: Span


Expression = (
    Name
    | Qualified
    | Number
    | String
    | Missing
    | Binary
    | Unary
    | In
    | Between
    | IsMissing
    | Call
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


@dataclass(frozen=True)
class LengthStatement:
    """LENGTH name $ 10 count 8: (name, is_text, length) for each variable."""

    entries: tuple[tuple[str, bool, int], ...]
    span: Span


@dataclass(frozen=True)
class FormatStatement:
    """FORMAT a b date9. c;: the variables it names. A format changes how
    SAS displays a value, never the value."""

    names: tuple[str, ...]
    span: Span


@dataclass(frozen=True)
class DatalinesStatement:
    """DATALINES (or CARDS, LINES, and their 4 forms), with the data lines
    that follow it, as text."""

    text: str
    span: Span
    # The line of the first data line, for messages.
    line: int


@dataclass(frozen=True)
class Assignment:
    name: str
    value: Expression
    span: Span


@dataclass(frozen=True)
class SubsettingIf:
    """IF condition; keeps only the rows for which it is true."""

    condition: Expression
    span: Span


@dataclass(frozen=True)
class Delete:
    span: Span


@dataclass(frozen=True)
class Block:
    """DO; ... END;"""

    statements: tuple["Statement", ...]
    span: Span


@dataclass(frozen=True)
class IfThen:
    """IF condition THEN statement, with the ELSE statement that follows it,
    if any. An ELSE IF chain is an IfThen in ``otherwise``."""

    condition: Expression
    then: "Statement"
    otherwise: "Statement | None"
    span: Span


Statement = (
    SetStatement
    | WhereStatement
    | KeepStatement
    | DropStatement
    | RenameStatement
    | LengthStatement
    | FormatStatement
    | DatalinesStatement
    | Assignment
    | SubsettingIf
    | Delete
    | Block
    | IfThen
)


@dataclass(frozen=True)
class DataStep:
    outputs: tuple[Dataset, ...]
    statements: tuple[Statement, ...]
    span: Span


@dataclass(frozen=True)
class ByVariable:
    name: str
    descending: bool


@dataclass(frozen=True)
class ProcSort:
    """PROC SORT. Without DATA=, it sorts the data set created last; without
    OUT=, the sorted data set replaces the one it sorts."""

    data: Dataset | None
    out: Dataset | None
    by: tuple[ByVariable, ...]
    nodupkey: bool
    wheres: tuple[WhereStatement, ...]
    span: Span


@dataclass(frozen=True)
class StatisticRequest:
    """statistic(variables)=names in an OUTPUT statement. Without
    variables, the statistic is for the VAR variables; without names, the
    output variables take the analysis variables' names."""

    statistic: str
    variables: tuple[str, ...]
    names: tuple[str, ...]
    span: Span


@dataclass(frozen=True)
class OutputStatement:
    out: Dataset | None
    statistics: tuple[StatisticRequest, ...]
    span: Span


@dataclass(frozen=True)
class ProcMeans:
    """PROC MEANS or PROC SUMMARY (``procedure``), which differ only in what
    they print. ``classes`` holds each CLASS variable and whether its
    statement has the MISSING option."""

    procedure: str
    data: Dataset | None
    nway: bool
    missing: bool
    by: tuple[ByVariable, ...]
    classes: tuple[tuple[str, bool], ...]
    variables: tuple[str, ...]
    outputs: tuple[OutputStatement, ...]
    wheres: tuple[WhereStatement, ...]
    span: Span


@dataclass(frozen=True)
class TablesStatement:
    """TABLES a a*b / options. Each request is the variables of one table;
    OUT= describes the last one."""

    requests: tuple[tuple[str, ...], ...]
    out: Dataset | None
    missing: bool
    outpct: bool
    span: Span


@dataclass(frozen=True)
class ProcFreq:
    data: Dataset | None
    by: tuple[ByVariable, ...]
    tables: tuple[TablesStatement, ...]
    wheres: tuple[WhereStatement, ...]
    span: Span


@dataclass(frozen=True)
class SqlItem:
    """One item of a SELECT list: ``expression`` is None for *."""

    expression: Expression | None
    alias: str | None
    span: Span


@dataclass(frozen=True)
class SqlTable:
    dataset: Dataset
    alias: str | None


@dataclass(frozen=True)
class SqlJoin:
    kind: str  # "inner" or "left"
    table: SqlTable
    condition: Expression


@dataclass(frozen=True)
class SqlOrder:
    expression: Expression
    descending: bool


@dataclass(frozen=True)
class SqlQuery:
    distinct: bool
    items: tuple[SqlItem, ...]
    table: SqlTable
    joins: tuple[SqlJoin, ...]
    where: Expression | None
    group_by: tuple[Expression, ...]
    having: Expression | None
    order_by: tuple[SqlOrder, ...]
    span: Span


@dataclass(frozen=True)
class SqlCreate:
    """CREATE TABLE target AS query."""

    target: Dataset
    query: SqlQuery
    span: Span


@dataclass(frozen=True)
class ProcSql:
    statements: tuple[SqlCreate, ...]
    span: Span


@dataclass(frozen=True)
class ProcPrint:
    """PROC PRINT: shows a data set (its VAR variables, in that order, when
    there is a VAR statement) and creates none."""

    data: Dataset | None
    variables: tuple[str, ...]
    wheres: tuple[WhereStatement, ...]
    span: Span


Step = DataStep | ProcSort | ProcMeans | ProcFreq | ProcSql | ProcPrint


@dataclass(frozen=True)
class Program:
    steps: tuple[Step, ...]
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
# PROC SORT options that do not change the sorted data: they control
# performance, stability (Spark's sort is not stable either way), or file
# attributes.
_SORT_OPTIONS_WITHOUT_EFFECT = frozenset(
    {
        "DATECOPY",
        "EQUALS",
        "FORCE",
        "NOEQUALS",
        "NOTHREADS",
        "OVERWRITE",
        "PRESORTED",
        "SIZE",
        "SORTSIZE",
        "TAGSORT",
        "THREADS",
    }
)
# The statistics PROC MEANS can compute. In the PROC statement they choose
# what is printed, not what an OUTPUT statement stores.
_MEANS_STATISTICS = frozenset(
    {
        "CLM",
        "CSS",
        "CV",
        "KURTOSIS",
        "KURT",
        "LCLM",
        "MAX",
        "MEAN",
        "MIN",
        "MODE",
        "N",
        "NMISS",
        "RANGE",
        "SKEWNESS",
        "SKEW",
        "STDDEV",
        "STD",
        "STDERR",
        "SUM",
        "SUMWGT",
        "UCLM",
        "USS",
        "VAR",
        "MEDIAN",
        "P1",
        "P5",
        "P10",
        "P20",
        "P25",
        "P30",
        "P40",
        "P50",
        "P60",
        "P70",
        "P75",
        "P80",
        "P90",
        "P95",
        "P99",
        "Q1",
        "Q3",
        "QRANGE",
        "PROBT",
        "PRT",
        "T",
    }
)
# PROC MEANS options that change only the printed output, or performance.
_MEANS_OPTIONS_WITHOUT_EFFECT = frozenset(
    {
        "ALPHA",
        "FW",
        "MAXDEC",
        "NONOBS",
        "NOPRINT",
        "NOTHREADS",
        "PCTLDEF",
        "PRINT",
        "PRINTALL",
        "PRINTALLTYPES",
        "PRINTIDS",
        "PRINTIDVARS",
        "QMARKERS",
        "QMETHOD",
        "QNTLDEF",
        "SUMSIZE",
        "THREADS",
    }
)
# Options with a value, accepted only with the value that is the default.
_MEANS_DEFAULT_VALUES = {"VARDEF": "DF", "ORDER": "UNFORMATTED"}
# PROC SQL options that change only the printed output, or messages.
_SQL_OPTIONS_WITHOUT_EFFECT = frozenset(
    {
        "DOUBLE",
        "ERRORSTOP",
        "EXEC",
        "FEEDBACK",
        "NODOUBLE",
        "NOERRORSTOP",
        "NOFEEDBACK",
        "NONUMBER",
        "NOPRINT",
        "NOSTIMER",
        "NOTHREADS",
        "NOWARNRECURS",
        "NUMBER",
        "PRINT",
        "STIMER",
        "THREADS",
        "WARNRECURS",
    }
)
# Words that end a table reference in a FROM clause.
_SQL_CLAUSE_WORDS = (
    "CROSS",
    "EXCEPT",
    "FULL",
    "GROUP",
    "HAVING",
    "INNER",
    "INTERSECT",
    "JOIN",
    "LEFT",
    "NATURAL",
    "ON",
    "ORDER",
    "OUTER",
    "RIGHT",
    "UNION",
    "WHERE",
)
_OTHER_JOINS = ("RIGHT", "FULL", "CROSS", "NATURAL")
# PROC PRINT options that change only the printout. (OBS= here labels the
# observation column; it does not limit the rows.)
_PRINT_OPTIONS_WITHOUT_EFFECT = frozenset(
    {
        "BLANKLINE",
        "CONTENTS",
        "DOUBLE",
        "GRANDTOTAL_LABEL",
        "HEADING",
        "LABEL",
        "N",
        "NOOBS",
        "NOSUMLABEL",
        "OBS",
        "ROUND",
        "ROWS",
        "SPLIT",
        "SUMLABEL",
        "UNIFORM",
        "WIDTH",
    }
)
_DATALINES_KEYWORDS = (
    "DATALINES",
    "CARDS",
    "LINES",
    "DATALINES4",
    "CARDS4",
    "LINES4",
)
# PROC FREQ and TABLES options that change only the printed output.
_FREQ_OPTIONS_WITHOUT_EFFECT = frozenset({"COMPRESS", "NLEVELS", "NOPRINT", "PAGE"})
_TABLES_OPTIONS_WITHOUT_EFFECT = frozenset(
    {
        "CROSSLIST",
        "LIST",
        "MISSPRINT",
        "NOCOL",
        "NOCUM",
        "NOFREQ",
        "NOPERCENT",
        "NOPRINT",
        "NOROW",
        "NOSPARSE",
    }
)


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
                step = self.proc_step()
                if step is not None:
                    steps.append(step)
            elif first.is_keyword(*_STEP_ENDS):
                self.index += 1  # a RUN with no step to end does nothing
            elif first.kind is Kind.DATA:
                # Data lines after a DATALINES statement outside a DATA step,
                # which is reported: they are data, not statements.
                self.index += 1
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
        while self.index < len(self.statements):
            first = self.statements[self.index][0]
            if first.is_keyword(*_STEP_KEYWORDS):
                break  # the next step starts; SAS ends this one
            if first.is_keyword(*_STEP_ENDS):
                self.index += 1
                break
            parsed = self.statement()
            if parsed is not None:
                statements.append(parsed)
        last = self.statements[self.index - 1]
        return DataStep(outputs, tuple(statements), self.span(header[0], last[-1]))

    def proc_step(self) -> Step | None:
        header = self.statements[self.index]
        if len(header) < 2:
            raise self.error("PROC without a procedure name", header[0])
        self.index += 1
        body: list[list[Token]] = []
        last = header[-1]
        while self.index < len(self.statements):
            statement = self.statements[self.index]
            if statement[0].is_keyword(*_STEP_KEYWORDS):
                break
            self.index += 1
            last = statement[-1]
            if statement[0].is_keyword(*_STEP_ENDS):
                break
            body.append(statement)
        span = self.span(header[0], last)
        if header[1].is_keyword("SORT"):
            return self.proc_sort(header, body, span)
        if header[1].is_keyword("MEANS", "SUMMARY"):
            return self.proc_means(header, body, span)
        if header[1].is_keyword("FREQ"):
            return self.proc_freq(header, body, span)
        if header[1].is_keyword("SQL"):
            return self.proc_sql(header, body, span)
        if header[1].is_keyword("PRINT"):
            return self.proc_print(header, body, span)
        self.unsupported(f"PROC {header[1].text.upper()} step", header)
        return None

    def proc_print(
        self, header: list[Token], body: list[list[Token]], span: Span
    ) -> ProcPrint:
        tokens = _Tokens(header[2:], self)
        data = None
        while not tokens.at_end():
            option = tokens.take_name("a PROC PRINT option")
            keyword = option.text.upper()
            has_value = tokens.peek().is_symbol("=")
            if keyword == "DATA" and has_value:
                tokens.take()
                data = self.dataset(tokens)
                continue
            if has_value:
                tokens.take()
                tokens.skip_option_value()
            if keyword not in _PRINT_OPTIONS_WITHOUT_EFFECT:
                name = f"{keyword}=" if has_value else keyword
                self.unsupported(
                    f"PROC PRINT option {name}",
                    [option],
                    "SparkShift supports DATA= and the options that change only "
                    "the printout, such as NOOBS and LABEL.",
                )
        variables: list[str] = []
        wheres: list[WhereStatement] = []
        for statement in body:
            first = statement[0]
            rest = _Tokens(statement[1:], self)
            if first.is_keyword("VAR"):
                variables += self.names(rest, "VAR")
            elif first.is_keyword("WHERE"):
                condition = self.expression(rest)
                rest.expect_end()
                wheres.append(
                    WhereStatement(condition, self.span(first, statement[-1]))
                )
            else:
                self.unsupported(
                    f"{first.text.upper()} statement in PROC PRINT", statement
                )
        return ProcPrint(data, tuple(variables), tuple(wheres), span)

    def proc_means(
        self, header: list[Token], body: list[list[Token]], span: Span
    ) -> ProcMeans:
        procedure = header[1].text.upper()
        tokens = _Tokens(header[2:], self)
        data = None
        nway = missing = False
        while not tokens.at_end():
            option = tokens.take_name(f"a PROC {procedure} option")
            keyword = option.text.upper()
            has_value = tokens.peek().is_symbol("=")
            if keyword == "DATA" and has_value:
                tokens.take()
                data = self.dataset(tokens)
                continue
            value = self.option_value(tokens) if has_value else None
            if keyword == "NWAY" and not has_value:
                nway = True
            elif keyword == "MISSING" and not has_value:
                missing = True
            elif not self.harmless_option(
                keyword,
                value,
                # In the PROC statement, statistics choose what is printed.
                _MEANS_OPTIONS_WITHOUT_EFFECT | _MEANS_STATISTICS,
                _MEANS_DEFAULT_VALUES,
            ):
                name = f"{keyword}={value}" if has_value else keyword
                self.unsupported(
                    f"PROC {procedure} option {name}",
                    [option],
                    "SparkShift supports DATA=, NWAY, and MISSING.",
                )
        by: list[ByVariable] = []
        classes: list[tuple[str, bool]] = []
        variables: list[str] = []
        outputs: list[OutputStatement] = []
        wheres: list[WhereStatement] = []
        for statement in body:
            first = statement[0]
            statement_span = self.span(first, statement[-1])
            names, options = _split_options(statement[1:])
            rest = _Tokens(names, self)
            if first.is_keyword("BY"):
                if by:
                    raise self.error(
                        f"PROC {procedure} with a second BY statement", first
                    )
                by = self.by_variables(rest)
            elif first.is_keyword("CLASS"):
                class_missing = self.class_options(options)
                classes += [(n, class_missing) for n in self.names(rest, "CLASS")]
            elif first.is_keyword("VAR"):
                if options:
                    self.unsupported("VAR statement options", options)
                variables += self.names(rest, "VAR")
            elif first.is_keyword("OUTPUT"):
                outputs.append(self.output_statement(statement, statement_span))
            elif first.is_keyword("WHERE"):
                where = _Tokens(statement[1:], self)
                condition = self.expression(where)
                where.expect_end()
                wheres.append(WhereStatement(condition, statement_span))
            else:
                self.unsupported(
                    f"{first.text.upper()} statement in PROC {procedure}", statement
                )
        return ProcMeans(
            procedure,
            data,
            nway,
            missing,
            tuple(by),
            tuple(classes),
            tuple(variables),
            tuple(outputs),
            tuple(wheres),
            span,
        )

    def proc_freq(
        self, header: list[Token], body: list[list[Token]], span: Span
    ) -> ProcFreq:
        tokens = _Tokens(header[2:], self)
        data = None
        while not tokens.at_end():
            option = tokens.take_name("a PROC FREQ option")
            keyword = option.text.upper()
            has_value = tokens.peek().is_symbol("=")
            if keyword == "DATA" and has_value:
                tokens.take()
                data = self.dataset(tokens)
                continue
            value = self.option_value(tokens) if has_value else None
            if not self.harmless_option(
                keyword, value, _FREQ_OPTIONS_WITHOUT_EFFECT, {"ORDER": "INTERNAL"}
            ):
                name = f"{keyword}={value}" if has_value else keyword
                self.unsupported(
                    f"PROC FREQ option {name}",
                    [option],
                    "SparkShift supports DATA= and options that only change what "
                    "is printed.",
                )
        by: list[ByVariable] = []
        tables: list[TablesStatement] = []
        wheres: list[WhereStatement] = []
        for statement in body:
            first = statement[0]
            statement_span = self.span(first, statement[-1])
            if first.is_keyword("BY"):
                if by:
                    raise self.error("PROC FREQ with a second BY statement", first)
                by = self.by_variables(_Tokens(statement[1:], self))
            elif first.is_keyword("TABLES", "TABLE"):
                tables.append(self.tables_statement(statement, statement_span))
            elif first.is_keyword("WHERE"):
                where = _Tokens(statement[1:], self)
                condition = self.expression(where)
                where.expect_end()
                wheres.append(WhereStatement(condition, statement_span))
            else:
                self.unsupported(
                    f"{first.text.upper()} statement in PROC FREQ", statement
                )
        return ProcFreq(data, tuple(by), tuple(tables), tuple(wheres), span)

    def tables_statement(self, statement: list[Token], span: Span) -> TablesStatement:
        """TABLES a a*b / OUT=data-set MISSING OUTPCT."""
        request_tokens, options = _split_options(statement[1:])
        if not request_tokens:
            raise self.error("TABLES names no variables", statement[0])
        tokens = _Tokens(request_tokens, self)
        requests: list[tuple[str, ...]] = []
        while not tokens.at_end():
            if tokens.peek().kind is not Kind.NAME:
                # (a b)*c, or a variable list such as a--c.
                self.unsupported(
                    "table request with parentheses or a variable list",
                    tokens.rest(),
                    "Write each table out, as in TABLES a*c b*c.",
                )
                break
            names = [tokens.take().text]
            while tokens.peek().is_symbol("*"):
                tokens.take()
                names.append(tokens.take_name("a variable after *").text)
            if tokens.peek().is_symbol("-", "--", ":"):
                self.unsupported(
                    "table request with parentheses or a variable list",
                    tokens.rest(),
                    "Write each table out, as in TABLES a*c b*c.",
                )
                break
            requests.append(tuple(names))
        out = None
        missing = outpct = False
        option_tokens = _Tokens(options, self)
        while not option_tokens.at_end():
            option = option_tokens.take_name("a TABLES option")
            keyword = option.text.upper()
            if keyword == "OUT" and option_tokens.peek().is_symbol("="):
                option_tokens.take()
                out = self.dataset(option_tokens)
                continue
            value = (
                self.option_value(option_tokens)
                if option_tokens.peek().is_symbol("=")
                else None
            )
            if keyword == "MISSING" and value is None:
                missing = True
            elif keyword == "OUTPCT" and value is None:
                outpct = True
            elif not (value is None and keyword in _TABLES_OPTIONS_WITHOUT_EFFECT):
                name = f"{keyword}={value}" if value is not None else keyword
                self.unsupported(f"TABLES option {name}", [option])
        return TablesStatement(tuple(requests), out, missing, outpct, span)

    def proc_sql(
        self, header: list[Token], body: list[list[Token]], span: Span
    ) -> ProcSql:
        tokens = _Tokens(header[2:], self)
        while not tokens.at_end():
            option = tokens.take_name("a PROC SQL option")
            keyword = option.text.upper()
            value = self.option_value(tokens) if tokens.peek().is_symbol("=") else None
            if not (value is None and keyword in _SQL_OPTIONS_WITHOUT_EFFECT):
                name = f"{keyword}={value}" if value is not None else keyword
                self.unsupported(f"PROC SQL option {name}", [option])
        statements = []
        for statement in body:
            created = self.sql_statement(statement)
            if created is not None:
                statements.append(created)
        return ProcSql(tuple(statements), span)

    def sql_statement(self, statement: list[Token]) -> SqlCreate | None:
        """CREATE TABLE name AS SELECT ...; other statements are reported."""
        first = statement[0]
        span = self.span(first, statement[-1])
        tokens = _Tokens(statement, self)
        if not (first.is_keyword("CREATE") and tokens.peek(1).is_keyword("TABLE")):
            hint = (
                "A query on its own prints a report; write CREATE TABLE name AS "
                "SELECT ... to create a data set."
                if first.is_keyword("SELECT")
                else None
            )
            self.unsupported(
                f"{first.text.upper()} statement in PROC SQL", statement, hint
            )
            return None
        tokens.take()
        tokens.take()
        target = self.dataset(tokens)
        if not tokens.peek().is_keyword("AS"):
            self.unsupported(
                "CREATE TABLE without AS SELECT",
                statement,
                "SparkShift creates tables from a query: CREATE TABLE name AS SELECT.",
            )
            return None
        tokens.take()
        query = self.sql_query(tokens)
        return None if query is None else SqlCreate(target, query, span)

    def sql_query(self, tokens: "_Tokens") -> SqlQuery | None:
        """SELECT ... FROM ... [JOIN ... ON ...] [WHERE] [GROUP BY] [HAVING]
        [ORDER BY]. Returns None after reporting a construct it skips."""
        select = tokens.take()
        if not select.is_keyword("SELECT"):
            raise self.error("Expected SELECT", select)
        distinct = tokens.peek().is_keyword("DISTINCT")
        if distinct:
            tokens.take()
        items = [self.sql_item(tokens)]
        while tokens.peek().is_symbol(","):
            tokens.take()
            items.append(self.sql_item(tokens))
        if not tokens.peek().is_keyword("FROM"):
            found = tokens.peek().text or "end of statement"
            raise self.error(f"Expected FROM, found {found!r}", tokens.peek())
        tokens.take()
        table = self.sql_table(tokens)
        joins = []
        while True:
            kind = self.join_kind(tokens)
            if kind is None:
                break
            joined = self.sql_table(tokens)
            on = tokens.take()
            if not on.is_keyword("ON"):
                raise self.error("Expected ON after the joined table", on)
            joins.append(SqlJoin(kind, joined, self.expression(tokens)))
        if tokens.peek().is_symbol(",") or tokens.peek().is_keyword(*_OTHER_JOINS):
            self.unsupported(
                f"{tokens.peek().text.upper()} join",
                tokens.rest(),
                "SparkShift supports INNER JOIN and LEFT JOIN with ON.",
            )
            return None
        where = having = None
        group_by: list[Expression] = []
        order_by: list[SqlOrder] = []
        if tokens.peek().is_keyword("WHERE"):
            tokens.take()
            where = self.expression(tokens)
        if tokens.peek().is_keyword("GROUP"):
            tokens.take()
            self.expect_keyword(tokens, "BY")
            group_by = self.sql_list(tokens)
        if tokens.peek().is_keyword("HAVING"):
            tokens.take()
            having = self.expression(tokens)
        if tokens.peek().is_keyword("ORDER"):
            tokens.take()
            self.expect_keyword(tokens, "BY")
            for expression in self.sql_list(tokens, orders=True):
                order_by.append(expression)
        if not tokens.at_end():
            token = tokens.peek()
            self.unsupported(
                f"{token.text.upper()} in a query",
                tokens.rest(),
                "SparkShift supports SELECT, FROM, JOIN, WHERE, GROUP BY, HAVING, "
                "and ORDER BY.",
            )
            return None
        span = self.span(select, tokens.previous())
        return SqlQuery(
            distinct,
            tuple(items),
            table,
            tuple(joins),
            where,
            tuple(group_by),
            having,
            tuple(order_by),
            span,
        )

    def sql_item(self, tokens: "_Tokens") -> SqlItem:
        """An item of the SELECT list: *, or an expression with an optional AS
        alias and column modifiers such as FORMAT=."""
        first = tokens.peek()
        if first.is_symbol("*"):
            tokens.take()
            return SqlItem(None, None, self.span(first, first))
        expression = self.expression(tokens)
        alias = None
        if tokens.peek().is_keyword("AS"):
            tokens.take()
            alias_token = tokens.take_name("a column name after AS")
            if alias_token.is_keyword("FROM"):
                raise self.error("Expected a column name after AS", alias_token)
            alias = alias_token.text
        while tokens.peek().kind is Kind.NAME and tokens.peek(1).is_symbol("="):
            modifier = tokens.take()
            self.option_value(tokens)
            if not modifier.is_keyword("FORMAT", "INFORMAT", "LABEL"):
                self.unsupported(
                    f"column modifier {modifier.text.upper()}=", [modifier]
                )
        return SqlItem(expression, alias, self.span(first, tokens.previous()))

    def sql_table(self, tokens: "_Tokens") -> SqlTable:
        dataset = self.dataset(tokens)
        alias = None
        if tokens.peek().is_keyword("AS"):
            tokens.take()
            alias_token = tokens.take_name("a table alias after AS")
            if alias_token.is_keyword(*_SQL_CLAUSE_WORDS):
                raise self.error("Expected a table alias after AS", alias_token)
            alias = alias_token.text
        elif tokens.peek().kind is Kind.NAME and not tokens.peek().is_keyword(
            *_SQL_CLAUSE_WORDS
        ):
            alias = tokens.take().text
        return SqlTable(dataset, alias)

    def join_kind(self, tokens: "_Tokens") -> str | None:
        """INNER JOIN, JOIN, or LEFT [OUTER] JOIN, taken from the tokens."""
        if tokens.peek().is_keyword("JOIN"):
            tokens.take()
            return "inner"
        if tokens.peek().is_keyword("INNER") and tokens.peek(1).is_keyword("JOIN"):
            tokens.take()
            tokens.take()
            return "inner"
        if tokens.peek().is_keyword("LEFT"):
            tokens.take()
            if tokens.peek().is_keyword("OUTER"):
                tokens.take()
            self.expect_keyword(tokens, "JOIN")
            return "left"
        return None

    def sql_list(self, tokens: "_Tokens", *, orders: bool = False) -> list:
        """Expressions separated by commas, as in GROUP BY; with ``orders``,
        each may be followed by ASC or DESC."""
        found: list = []
        while True:
            expression = self.expression(tokens)
            if orders:
                descending = tokens.peek().is_keyword("DESC")
                if descending or tokens.peek().is_keyword("ASC"):
                    tokens.take()
                found.append(SqlOrder(expression, descending))
            else:
                found.append(expression)
            if not tokens.peek().is_symbol(","):
                return found
            tokens.take()

    def expect_keyword(self, tokens: "_Tokens", keyword: str) -> None:
        token = tokens.take()
        if not token.is_keyword(keyword):
            found = token.text or "end of statement"
            raise self.error(f"Expected {keyword}, found {found!r}", token)

    def option_value(self, tokens: "_Tokens") -> str:
        """The value of an option after its equals sign, upper case: one
        token or a parenthesized group, with the tokens written right after
        it, as in the size 512M or the format DOLLAR8.2."""
        tokens.take()
        start = tokens.index
        tokens.skip_option_value()
        while (
            not tokens.at_end()
            and tokens.adjacent(tokens.previous())
            and (
                tokens.peek().kind in (Kind.NAME, Kind.NUMBER)
                or tokens.peek().is_symbol(".")
            )
        ):
            tokens.take()
        return "".join(
            token.text for token in tokens.tokens[start : tokens.index]
        ).upper()

    def harmless_option(
        self,
        keyword: str,
        value: str | None,
        without_effect: frozenset[str],
        defaults: dict[str, str],
    ) -> bool:
        """Whether a PROC MEANS or FREQ option leaves the output data set
        unchanged."""
        if keyword in defaults:
            return value == defaults[keyword]
        return keyword in without_effect

    def class_options(self, options: list[Token]) -> bool:
        """The options of a CLASS statement, after the slash: whether it has
        MISSING. Options about formats and printing are reported."""
        missing = False
        tokens = _Tokens(options, self)
        while not tokens.at_end():
            option = tokens.take_name("a CLASS option")
            keyword = option.text.upper()
            value = self.option_value(tokens) if tokens.peek().is_symbol("=") else None
            if keyword == "MISSING" and value is None:
                missing = True
            elif not (
                keyword in ("ASCENDING", "ASCEND", "GROUPINTERNAL") and value is None
            ) and not (keyword == "ORDER" and value == "UNFORMATTED"):
                name = f"{keyword}={value}" if value is not None else keyword
                self.unsupported(f"CLASS option {name}", [option])
        return missing

    def output_statement(self, statement: list[Token], span: Span) -> OutputStatement:
        """OUTPUT OUT=data-set statistic(variables)=names ... / options."""
        names, options = _split_options(statement[1:])
        tokens = _Tokens(names, self)
        out = None
        statistics: list[StatisticRequest] = []
        while not tokens.at_end():
            keyword = tokens.take_name("OUT= or a statistic")
            upper = keyword.text.upper()
            if upper == "OUT" and tokens.peek().is_symbol("="):
                tokens.take()
                out = self.dataset(tokens)
                continue
            if upper in ("IDGROUP", "IDGRP", "MAXID", "MINID"):
                self.unsupported(f"{upper} in an OUTPUT statement", [keyword])
                break
            variables: tuple[str, ...] = ()
            if tokens.peek().is_symbol("("):
                tokens.take()
                variables = self.names(tokens, f"{upper}(...)")
                tokens.expect_symbol(")")
            tokens.expect_symbol("=")
            outputs = []
            while tokens.peek().kind is Kind.NAME and not tokens.peek(1).is_symbol(
                "=", "("
            ):
                outputs.append(tokens.take().text)
            statistics.append(
                StatisticRequest(
                    upper,
                    variables,
                    tuple(outputs),
                    self.span(keyword, tokens.previous()),
                )
            )
        option_tokens = _Tokens(options, self)
        while not option_tokens.at_end():
            option = option_tokens.take_name("an OUTPUT option")
            if not option.is_keyword("AUTOLABEL", "NOINHERIT", "INHERIT", "KEEPLEN"):
                self.unsupported(f"OUTPUT option {option.text.upper()}", [option])
        return OutputStatement(out, tuple(statistics), span)

    def proc_sort(
        self, header: list[Token], body: list[list[Token]], span: Span
    ) -> ProcSort:
        tokens = _Tokens(header[2:], self)
        data = out = None
        nodupkey = False
        while not tokens.at_end():
            option = tokens.take_name("a PROC SORT option")
            keyword = option.text.upper()
            has_value = tokens.peek().is_symbol("=")
            if keyword in ("DATA", "OUT") and has_value:
                tokens.take()
                dataset = self.dataset(tokens)
                if keyword == "DATA":
                    data = dataset
                else:
                    out = dataset
                continue
            if has_value:
                tokens.take()
                tokens.skip_option_value()
                value = tokens.previous()
                if value.kind is Kind.NUMBER and tokens.adjacent(value):
                    tokens.take()  # the unit of a size such as 512M
            if keyword == "NODUPKEY":
                nodupkey = True
            elif keyword not in _SORT_OPTIONS_WITHOUT_EFFECT:
                name = f"{keyword}=" if has_value else keyword
                self.unsupported(
                    f"PROC SORT option {name}",
                    [option],
                    "SparkShift supports DATA=, OUT=, and NODUPKEY.",
                )
        by: list[ByVariable] = []
        wheres: list[WhereStatement] = []
        for statement in body:
            first = statement[0]
            rest = _Tokens(statement[1:], self)
            if first.is_keyword("BY"):
                if by:
                    raise self.error("PROC SORT with a second BY statement", first)
                by = self.by_variables(rest)
            elif first.is_keyword("WHERE"):
                condition = self.expression(rest)
                rest.expect_end()
                wheres.append(
                    WhereStatement(condition, self.span(first, statement[-1]))
                )
            else:
                self.unsupported(
                    f"{first.text.upper()} statement in PROC SORT", statement
                )
        if not by:
            raise self.error("PROC SORT without a BY statement", header[0])
        return ProcSort(data, out, tuple(by), nodupkey, tuple(wheres), span)

    def by_variables(self, tokens: "_Tokens") -> list[ByVariable]:
        """BY a DESCENDING b: DESCENDING applies to the variable after it."""
        variables = []
        while not tokens.at_end():
            name = tokens.take_name("a BY variable")
            if name.is_keyword("NOTSORTED", "GROUPFORMAT") and variables:
                self.unsupported(f"BY option {name.text.upper()}", [name])
                continue
            descending = name.is_keyword("DESCENDING") and not tokens.at_end()
            if descending:
                name = tokens.take_name("a variable after DESCENDING")
            variables.append(ByVariable(name.text, descending))
        if not variables:
            raise self.error("BY names no variables", tokens.previous())
        return variables

    def statement(self) -> Statement | None:
        """Parse the next DATA step statement, with the statements an IF or
        DO takes with it (its ELSE, or its block up to END)."""
        tokens = self.statements[self.index]
        self.index += 1
        return self.data_statement(tokens)

    def data_statement(self, statement: list[Token]) -> Statement | None:
        first = statement[0]
        tokens = _Tokens(statement[1:], self)
        span = self.span(statement[0], statement[-1])
        if first.is_keyword("IF"):
            return self.if_statement(statement)
        if first.is_keyword("DO"):
            return self.block(statement)
        if first.is_keyword("DELETE") and len(statement) == 1:
            return Delete(span)
        if first.is_keyword("LENGTH"):
            return LengthStatement(self.lengths(tokens), span)
        if first.is_keyword("FORMAT"):
            return self.format_statement(statement, span)
        if first.is_keyword(*_DATALINES_KEYWORDS) and len(statement) == 1:
            return self.datalines(first, span)
        if first.is_keyword("ELSE"):
            raise self.error("ELSE without a matching IF", first)
        if first.is_keyword("END") and len(statement) == 1:
            raise self.error("END without a matching DO", first)
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
            tokens.take()
            value = self.expression(tokens)
            tokens.expect_end()
            return Assignment(first.text, value, span)
        self.unsupported(self.statement_name(statement), statement)
        return None

    def if_statement(self, statement: list[Token]) -> Statement | None:
        """IF condition; (subsetting) or IF condition THEN statement, with the
        ELSE statement that follows, if any."""
        tokens = _Tokens(statement[1:], self)
        condition = self.expression(tokens)
        span = self.span(statement[0], statement[-1])
        if tokens.at_end():
            return SubsettingIf(condition, span)
        then = tokens.take()
        if not then.is_keyword("THEN"):
            raise self.error(f"Expected THEN, found {then.text!r}", then)
        if tokens.at_end():
            raise self.error("THEN without a statement", then)
        action = self.data_statement(tokens.rest())
        otherwise = None
        has_else = False
        if self.index < len(self.statements):
            following = self.statements[self.index]
            if following[0].is_keyword("ELSE"):
                if len(following) == 1:
                    raise self.error("ELSE without a statement", following[0])
                self.index += 1
                has_else = True
                otherwise = self.data_statement(following[1:])
        if action is None or (has_else and otherwise is None):
            return None  # an unsupported branch is already reported
        return IfThen(condition, action, otherwise, span)

    def block(self, statement: list[Token]) -> Statement | None:
        """DO; ... END;. DO loops (DO i = ..., DO WHILE, DO UNTIL) are
        reported, and their statements skipped."""
        loop = len(statement) > 1
        if loop:
            self.unsupported(
                "DO loop",
                statement,
                "Loops run row by row; only DO; ... END; blocks are supported.",
            )
        statements: list[Statement] = []
        while self.index < len(self.statements):
            current = self.statements[self.index]
            first = current[0]
            if first.is_keyword("END") and len(current) == 1:
                self.index += 1
                end = current[0]
                if loop:
                    return None
                return Block(tuple(statements), self.span(statement[0], end))
            if first.is_keyword(*_STEP_KEYWORDS, *_STEP_ENDS):
                break
            parsed = self.statement()
            if parsed is not None:
                statements.append(parsed)
        raise self.error("DO without a matching END", statement[0])

    def lengths(self, tokens: "_Tokens") -> tuple[tuple[str, bool, int], ...]:
        """LENGTH a b $ 10 c 8: each length applies to the names before it."""
        entries: list[tuple[str, bool, int]] = []
        pending: list[str] = []
        while not tokens.at_end():
            token = tokens.take()
            if token.kind is Kind.NAME:
                pending.append(token.text)
                continue
            is_text = token.is_symbol("$")
            length = tokens.take() if is_text else token
            if (
                length.kind is not Kind.NUMBER
                or not length.text.isdigit()
                or not pending
            ):
                raise self.error("Expected a variable name or a length", length)
            entries += [(name, is_text, int(length.text)) for name in pending]
            pending = []
        if pending or not entries:
            raise self.error("LENGTH without a length", tokens.previous())
        return tuple(entries)

    def format_statement(
        self, statement: list[Token], span: Span
    ) -> FormatStatement | None:
        """FORMAT variables format. ...: the variables are the names that are
        not part of a format, such as date9., $20., 8.2, or dollar10.2."""
        names: list[str] = []
        tokens = _Tokens(statement[1:], self)
        while not tokens.at_end():
            token = tokens.take()
            if (
                token.is_symbol("$")
                or token.kind is Kind.NUMBER
                or (
                    token.kind is Kind.NAME
                    and tokens.adjacent(token)
                    and (
                        tokens.peek().is_symbol(".")
                        or tokens.peek().text.startswith(".")
                    )
                )
            ):
                self.skip_format(tokens)
            elif token.kind is Kind.NAME:
                if tokens.peek().is_symbol("-"):
                    self.unsupported("variable list in a FORMAT statement", statement)
                    return None
                names.append(token.text)
            else:
                self.unsupported("FORMAT statement", statement)
                return None
        return FormatStatement(tuple(names), span)

    def skip_format(self, tokens: "_Tokens") -> None:
        """The rest of a format name: tokens joined to it up to its period
        and decimals, as in $char20., date9., or comma10.2."""
        while not tokens.at_end() and tokens.adjacent(tokens.previous()):
            token = tokens.peek()
            if (
                (token.kind is Kind.NAME and not tokens.previous().text.endswith("."))
                or token.is_symbol(".")
                or token.kind is Kind.NUMBER
            ):
                tokens.take()
            else:
                break

    def datalines(self, keyword: Token, span: Span) -> DatalinesStatement:
        """DATALINES; and the DATA token of the lines after it."""
        following = self.statements[self.index : self.index + 1]
        lines = following[0] if following else []
        if len(lines) == 1 and lines[0].kind is Kind.DATA:
            self.index += 1
            data = lines[0]
            return DatalinesStatement(data.value, span, data.line)
        return DatalinesStatement("", span, keyword.line + 1)

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
        opening = tokens.expect_symbol("(")
        if tokens.peek().is_keyword("SELECT"):
            subquery = self.subquery(opening, tokens)
            return In(left, (subquery,), negated, self.span_of(left, subquery))
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
            following = tokens.peek()
            if (
                following.is_symbol(".")
                and tokens.adjacent(token)
                and tokens.peek(1).kind is Kind.NAME
                and tokens.peek(1).start == following.end
            ):
                # table.column, in PROC SQL.
                tokens.take()
                column = tokens.take()
                return Qualified(token.text, column.text, self.span(token, column))
            if token.is_keyword("CALCULATED") and following.kind is Kind.NAME:
                column = tokens.take()
                self.unsupported(
                    "CALCULATED",
                    [token, column],
                    "Repeat the expression the column is calculated from.",
                )
                return Name(column.text, self.span(token, column))
            return Name(token.text, span)
        if token.is_symbol("(") and tokens.peek().is_keyword("SELECT"):
            return self.subquery(token, tokens)
        if token.is_symbol("("):
            inner = self.expression(tokens)
            closing = tokens.expect_symbol(")")
            return _with_span(inner, self.span(token, closing))
        raise self.error(
            f"Unexpected {token.text or 'end of statement'!r} in an expression", token
        )

    def subquery(self, opening: Token, tokens: "_Tokens") -> Expression:
        """(SELECT ...) in PROC SQL: reported, and skipped to its closing
        parenthesis."""
        depth = 1
        while depth and not tokens.at_end():
            token = tokens.take()
            depth += token.is_symbol("(") - token.is_symbol(")")
        if depth:
            raise self.error("Expected ) to end the subquery", tokens.peek())
        span = self.span(opening, tokens.previous())
        self.unsupported("subquery", span, "Subqueries are not supported yet.")
        return Missing(span)

    def call(self, name: Token, tokens: "_Tokens") -> Expression:
        tokens.take()
        if tokens.peek().is_keyword("OF") and tokens.peek(1).kind is Kind.NAME:
            # SUM(OF x1-x3 y): a variable list.
            depth = 1
            while depth and not tokens.at_end():
                token = tokens.take()
                depth += token.is_symbol("(") - token.is_symbol(")")
            if depth:
                raise self.error("Expected ) to end the function call", tokens.peek())
            span = self.span(name, tokens.previous())
            self.unsupported(
                "OF variable list",
                span,
                "List the variables separated by commas, as in SUM(a, b, c).",
            )
            return Missing(span)
        if tokens.peek().is_symbol("*") and tokens.peek(1).is_symbol(")"):
            # COUNT(*), in PROC SQL.
            tokens.take()
            closing = tokens.take()
            return Call(name.text, (), self.span(name, closing), star=True)
        distinct = tokens.peek().is_keyword("DISTINCT") and not tokens.peek(
            1
        ).is_symbol(")", ",")
        if distinct:
            tokens.take()
        arguments: list[Expression] = []
        while not tokens.peek().is_symbol(")"):
            arguments.append(self.expression(tokens))
            if tokens.peek().is_symbol(","):
                tokens.take()
            elif not tokens.peek().is_symbol(")"):
                raise self.error("Expected , or ) in a function call", tokens.peek())
        closing = tokens.take()
        return Call(
            name.text, tuple(arguments), self.span(name, closing), distinct=distinct
        )

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


def _split_options(tokens: list[Token]) -> tuple[list[Token], list[Token]]:
    """A statement's tokens before and after the slash that starts its
    options."""
    for index, token in enumerate(tokens):
        if token.is_symbol("/"):
            return tokens[:index], tokens[index + 1 :]
    return tokens, []


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

    def rest(self) -> list[Token]:
        """The tokens not read yet, as a statement of their own."""
        return self.tokens[self.index :]

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
