"""Translate a parsed SAS program into SparkShift's IR.

Each DATA step becomes a named relation: a DataFrame variable in the
generated code. The rules SAS follows, and their sources in the SAS
documentation, are listed in docs/sas.md. In short:

- Variables are dropped, kept, and renamed in this order: options on the
  input data set (WHERE= and DROP=/KEEP= with the old names, then RENAME=),
  then the WHERE statement and the DROP, KEEP, and RENAME statements, then
  options on the output data set.
- A missing number is smaller than every number, and a comparison is always
  true or false, never missing. Missing text is blank, and trailing blanks do
  not count in a comparison.

Like the SQL translator, it works from an allowlist and reports every
unsupported construct at once.
"""

from dataclasses import dataclass

from sparkshift import ir
from sparkshift.diagnostics import Diagnostic
from sparkshift.errors import UnsupportedSQLError
from sparkshift.sas_parser import (
    Between,
    Binary,
    Call,
    Dataset,
    DataStep,
    DropOption,
    DropStatement,
    Expression,
    In,
    IsMissing,
    KeepOption,
    KeepStatement,
    Missing,
    Name,
    Number,
    Program,
    RenameOption,
    RenameStatement,
    SetStatement,
    Span,
    String,
    Unary,
    WhereOption,
    WhereStatement,
)

_COMPARISON_OPERATORS = {
    "=": ir.BinaryOperator.EQUAL,
    "^=": ir.BinaryOperator.NOT_EQUAL,
    "<": ir.BinaryOperator.LESS,
    "<=": ir.BinaryOperator.LESS_EQUAL,
    ">": ir.BinaryOperator.GREATER,
    ">=": ir.BinaryOperator.GREATER_EQUAL,
}
# The same comparison with its operands swapped: 3 < x is x > 3.
_SWAPPED = {"=": "=", "^=": "^=", "<": ">", "<=": ">=", ">": "<", ">=": "<="}

_KEEP_ORDER_HINT = (
    "SAS keeps variables in the data set's order; this code keeps them in the "
    "listed order. List them in the data set's order to match SAS exactly."
)
_TWO_VARIABLES_HINT = (
    "SAS compares numbers and text differently, and SparkShift cannot see "
    "which these variables are. Compare a variable with a constant."
)
_LIBRARY_HINT = (
    "SparkShift creates DataFrames; write them to a table yourself, for "
    "example with .write.saveAsTable(...)."
)


@dataclass(frozen=True)
class SasTranslation:
    """The data sets a program creates, in order, and anything the caller
    should review."""

    datasets: tuple[ir.Named, ...]
    warnings: tuple[Diagnostic, ...]


def translate_sas(program: Program, source: str) -> SasTranslation:
    """Translate a parsed program; ``source`` is its text, for messages.

    Raises:
        UnsupportedSQLError: the program uses constructs SparkShift cannot
            translate; ``issues`` lists all of them, including those the
            parser found.
    """
    return _SasTranslator(program, source).translation()


@dataclass(frozen=True)
class _Operand:
    """A comparison operand: a variable, a number, a text constant, or the
    missing value."""

    kind: str  # "variable", "number", "text", or "missing"
    expression: ir.Expression | None


class _SasTranslator:
    def __init__(self, program: Program, source: str) -> None:
        self.program = program
        self.source = source
        self.issues: list[Diagnostic] = list(program.issues)
        self.warnings: list[Diagnostic] = []
        # Data sets the program has created so far, by lower-case name.
        self.created: dict[str, ir.Named] = {}
        self.outputs: list[ir.Named] = []

    def translation(self) -> SasTranslation:
        for step in self.program.steps:
            self.data_step(step)
        if self.issues:
            raise UnsupportedSQLError(self.issues)
        return SasTranslation(tuple(self.outputs), tuple(self.warnings))

    # --- Steps ---------------------------------------------------------------

    def data_step(self, step: DataStep) -> None:
        issues_before = len(self.issues)
        output = self.output_dataset(step)
        sets = [s for s in step.statements if isinstance(s, SetStatement)]
        wheres = [s for s in step.statements if isinstance(s, WhereStatement)]
        if not sets:
            self.unsupported(
                "DATA step without a SET statement",
                step.span,
                "Only DATA steps that read a data set are supported.",
            )
        for extra in sets[1:]:
            self.unsupported("second SET statement", extra.span)
        for extra in wheres[1:]:
            self.unsupported(
                "second WHERE statement",
                extra.span,
                "Combine the conditions with AND in one WHERE statement.",
            )
        if not sets or len(self.issues) > issues_before:
            self.check_statements(step)
            return
        if len(sets[0].datasets) != 1:
            self.unsupported(
                "SET with several data sets",
                sets[0].span,
                "Concatenating data sets is not supported yet.",
            )
            self.check_statements(step)
            return

        source = sets[0].datasets[0]
        relation, filtered = self.input_dataset(source)
        if wheres and filtered:
            self.warn(
                "WHERE statement ignored",
                wheres[0].span,
                "SAS ignores it because the SET data set has a WHERE= option.",
            )
        elif wheres:
            condition = self.condition(wheres[0].condition)
            if condition is not None:
                relation = ir.Filter(relation, condition)

        # The DROP and KEEP statements, then the RENAME statement.
        keeps = [
            n for s in step.statements if isinstance(s, KeepStatement) for n in s.names
        ]
        drops = [
            n for s in step.statements if isinstance(s, DropStatement) for n in s.names
        ]
        renames = [
            pair
            for s in step.statements
            if isinstance(s, RenameStatement)
            for pair in s.pairs
        ]
        keep_spans = [s.span for s in step.statements if isinstance(s, KeepStatement)]
        if keeps:
            relation = self.keep(relation, keeps, keep_spans[0])
        if drops:
            relation = ir.DropColumns(relation, _unique(drops))
        if renames:
            relation = self.rename(relation, renames, step.span)

        # A step without a usable output data set has an issue already.
        assert output is not None
        relation = self.dataset_options(relation, output, output_side=True)[0]
        if len(self.issues) > issues_before:
            return
        named = ir.Named(output.member.lower(), relation)
        self.created[output.member.lower()] = named
        self.outputs.append(named)

    def check_statements(self, step: DataStep) -> None:
        """Report issues in the conditions of a step that is not translated."""
        for statement in step.statements:
            if isinstance(statement, WhereStatement):
                self.condition(statement.condition)

    def output_dataset(self, step: DataStep) -> Dataset | None:
        if len(step.outputs) != 1:
            what = (
                "DATA statement without a data set"
                if not step.outputs
                else ("DATA statement with several data sets")
            )
            self.unsupported(
                what, step.span, "Each DATA step must create exactly one data set."
            )
            return None
        output = step.outputs[0]
        if output.member.upper() == "_NULL_":
            self.unsupported(
                "DATA _NULL_ step",
                output.span,
                "It creates no data set, so there is no DataFrame to make.",
            )
            return None
        if output.library is not None and output.library.upper() != "WORK":
            self.unsupported(
                f"DATA step writing to library {output.library.upper()}",
                output.span,
                _LIBRARY_HINT,
            )
            return None
        return output

    def input_dataset(self, dataset: Dataset) -> tuple[ir.Relation, bool]:
        """The relation a SET data set reads, after its options, and whether
        it has a WHERE= option."""
        temporary = dataset.library is None or dataset.library.upper() == "WORK"
        key = dataset.member.lower()
        relation: ir.Relation
        if temporary and key in self.created:
            relation = self.created[key]
        elif temporary:
            relation = ir.TableScan((dataset.member,))
        else:
            assert dataset.library is not None
            relation = ir.TableScan((dataset.library, dataset.member))
        return self.dataset_options(relation, dataset, output_side=False)

    def dataset_options(
        self, relation: ir.Relation, dataset: Dataset, *, output_side: bool
    ) -> tuple[ir.Relation, bool]:
        """Apply a data set's options: WHERE= first (it uses the old names),
        then DROP= and KEEP= from left to right, then RENAME=."""
        options = dataset.options
        wheres = [o for o in options if isinstance(o, WhereOption)]
        if output_side:
            for option in wheres:
                self.unsupported(
                    "WHERE= option on the output data set",
                    option.span,
                    "Use a WHERE statement, or WHERE= on the SET data set.",
                )
        else:
            for extra in wheres[1:]:
                self.unsupported("second WHERE= option", extra.span)
            if wheres:
                condition = self.condition(wheres[0].condition)
                if condition is not None:
                    relation = ir.Filter(relation, condition)
        for option in options:
            if isinstance(option, KeepOption):
                relation = self.keep(relation, list(option.names), option.span)
            elif isinstance(option, DropOption):
                relation = ir.DropColumns(relation, _unique(list(option.names)))
        renames = [p for o in options if isinstance(o, RenameOption) for p in o.pairs]
        if renames:
            relation = self.rename(relation, renames, dataset.span)
        return relation, bool(wheres) and not output_side

    def keep(self, relation: ir.Relation, names: list[str], span: Span) -> ir.Relation:
        kept = _unique(names)
        if len(kept) > 1:
            self.warn("KEEP of several variables", span, _KEEP_ORDER_HINT)
        return ir.Project(relation, tuple(ir.Column((name,)) for name in kept))

    def rename(
        self, relation: ir.Relation, pairs: list[tuple[str, str]], span: Span
    ) -> ir.Relation:
        olds = [old.lower() for old, _ in pairs]
        news = [new.lower() for _, new in pairs]
        if len(set(olds)) < len(olds) or set(olds) & set(news):
            self.unsupported(
                "renames that swap or chain names",
                span,
                "Rename each variable once, to a name not renamed itself.",
            )
            return relation
        return ir.RenameByName(relation, tuple(pairs))

    # --- Conditions ----------------------------------------------------------

    def condition(self, expression: Expression) -> ir.Expression | None:
        """A WHERE condition, always true or false (never NULL), as SAS
        conditions are."""
        match expression:
            case Binary(op="AND" | "OR" as op, left=left, right=right):
                left_ir, right_ir = self.condition(left), self.condition(right)
                if left_ir is None or right_ir is None:
                    return None
                operator = (
                    ir.BinaryOperator.AND if op == "AND" else ir.BinaryOperator.OR
                )
                return ir.BinaryOp(operator, left_ir, right_ir)
            case Unary(op="NOT", operand=operand):
                inner = self.condition(operand)
                return None if inner is None else _not(inner)
            case Binary(op=op, left=left, right=right) if op in _COMPARISON_OPERATORS:
                return self.comparison(op, left, right, expression.span)
            case In():
                return self.in_list(expression)
            case Between():
                return self.between(expression)
            case IsMissing(expression=inner, negated=negated):
                operand = self.operand(inner)
                if operand is None:
                    return None
                if operand.kind != "variable":
                    self.unsupported("IS MISSING on a constant", expression.span)
                    return None
                assert operand.expression is not None
                missing = _is_missing(operand.expression)
                return _not(missing) if negated else missing
        self.unsupported(
            f"{_describe(expression)} as a condition",
            expression.span,
            "Use a comparison, for example: x ne 0.",
        )
        return None

    def comparison(
        self, op: str, left: Expression, right: Expression, span: Span
    ) -> ir.Expression | None:
        first, second = self.operand(left), self.operand(right)
        if first is None or second is None:
            return None
        if first.kind == "variable" and second.kind == "variable":
            self.unsupported("comparison of two variables", span, _TWO_VARIABLES_HINT)
            return None
        if second.kind == "variable":
            first, second, op = second, first, _SWAPPED[op]
        if first.kind != "variable":
            self.unsupported("comparison of two constants", span)
            return None
        variable = first.expression
        assert variable is not None
        if second.kind == "text":
            assert second.expression is not None
            return ir.BinaryOp(
                _COMPARISON_OPERATORS[op], _text(variable), second.expression
            )
        if second.kind == "missing":
            return _compare_with_missing(op, variable)
        assert second.expression is not None
        return _compare_number(op, variable, second.expression)

    def in_list(self, node: In) -> ir.Expression | None:
        operand = self.operand(node.expression)
        values = [self.operand(value) for value in node.values]
        if operand is None or None in values:
            return None
        if operand.kind != "variable":
            self.unsupported("IN on a constant", node.span)
            return None
        assert operand.expression is not None
        kinds = {value.kind for value in values if value is not None}
        if "variable" in kinds:
            self.unsupported(
                "variable in an IN list", node.span, "An IN list holds constants."
            )
            return None
        if "text" in kinds and kinds != {"text"}:
            self.unsupported("IN list of text and numbers", node.span)
            return None
        constants = [
            v.expression for v in values if v is not None and v.expression is not None
        ]
        result: ir.Expression
        if kinds == {"text"}:
            result = ir.InList(_text(operand.expression), tuple(constants))
        elif not constants:
            result = ir.IsNull(operand.expression)
        else:
            within = ir.InList(operand.expression, tuple(constants))
            if "missing" in kinds:
                result = ir.BinaryOp(
                    ir.BinaryOperator.OR, ir.IsNull(operand.expression), within
                )
            else:
                result = _present_and(operand.expression, within)
        return _not(result) if node.negated else result

    def between(self, node: Between) -> ir.Expression | None:
        operands = [self.operand(e) for e in (node.expression, node.low, node.high)]
        if None in operands:
            return None
        value, low, high = operands
        assert value is not None and low is not None and high is not None
        if value.kind != "variable" or {low.kind, high.kind} & {"variable", "missing"}:
            self.unsupported(
                "BETWEEN other than a variable between two constants", node.span
            )
            return None
        if low.kind != high.kind:
            self.unsupported("BETWEEN a number and text", node.span)
            return None
        assert value.expression and low.expression and high.expression
        result: ir.Expression
        if low.kind == "text":
            result = ir.Between(
                _text(value.expression), low.expression, high.expression
            )
        else:
            within = ir.Between(value.expression, low.expression, high.expression)
            result = _present_and(value.expression, within)
        return _not(result) if node.negated else result

    def operand(self, expression: Expression) -> _Operand | None:
        match expression:
            case Name(name=name):
                return _Operand("variable", ir.Column((name,)))
            case Number(value=value):
                return _Operand("number", ir.Literal(value))
            case Unary(op="-", operand=Number(value=value)):
                return _Operand("number", ir.Literal(-value))
            case String(value=value):
                # Trailing blanks do not count in SAS comparisons.
                return _Operand("text", ir.Literal(value.rstrip(" ")))
            case Missing():
                return _Operand("missing", None)
        self.unsupported(
            f"{_describe(expression)} in a condition",
            expression.span,
            "Only variables and constants are supported in conditions so far.",
        )
        return None

    # --- Diagnostics ---------------------------------------------------------

    def unsupported(self, message: str, span: Span, hint: str | None = None) -> None:
        self.issues.append(Diagnostic(message, self.fragment(span), hint))

    def warn(self, message: str, span: Span, hint: str) -> None:
        self.warnings.append(Diagnostic(message, self.fragment(span), hint))

    def fragment(self, span: Span) -> str:
        return " ".join(self.source[span.start : span.end].split())


# --- SAS semantics as Spark expressions -----------------------------------------


def _compare_number(
    op: str, variable: ir.Expression, number: ir.Expression
) -> ir.Expression:
    """variable op number, where a missing value is smaller than every number."""
    if op == "=":
        return ir.NullSafeEqual(variable, number)
    if op == "^=":
        return _not(ir.NullSafeEqual(variable, number))
    comparison = ir.BinaryOp(_COMPARISON_OPERATORS[op], variable, number)
    if op in ("<", "<="):
        # Missing is smaller, so the comparison is true.
        return ir.BinaryOp(ir.BinaryOperator.OR, ir.IsNull(variable), comparison)
    return _present_and(variable, comparison)


def _compare_with_missing(op: str, variable: ir.Expression) -> ir.Expression:
    """variable op ., where nothing is smaller than the missing value."""
    if op in ("=", "<="):
        return ir.IsNull(variable)
    if op in ("^=", ">"):
        return ir.IsNull(variable, negated=True)
    return ir.Literal(op == ">=")


def _text(variable: ir.Expression) -> ir.Expression:
    """A text variable as SAS compares it: missing is blank, and trailing
    blanks do not count."""
    trimmed = ir.FunctionCall("rtrim", (variable,))
    return ir.FunctionCall("coalesce", (trimmed, ir.Literal("")))


def _is_missing(variable: ir.Expression) -> ir.Expression:
    """Missing for a number or text: NULL, or blank text. A number is never
    blank, so this works whatever the variable's type."""
    blank = ir.BinaryOp(
        ir.BinaryOperator.EQUAL, ir.FunctionCall("rtrim", (variable,)), ir.Literal("")
    )
    return ir.BinaryOp(ir.BinaryOperator.OR, ir.IsNull(variable), blank)


def _present_and(variable: ir.Expression, condition: ir.Expression) -> ir.Expression:
    """condition, and false (not NULL) when the variable is missing."""
    return ir.BinaryOp(
        ir.BinaryOperator.AND, ir.IsNull(variable, negated=True), condition
    )


def _not(condition: ir.Expression) -> ir.Expression:
    return ir.UnaryOp(ir.UnaryOperator.NOT, condition)


def _unique(names: list[str]) -> tuple[str, ...]:
    """Names in order without repeats, ignoring case as SAS does."""
    seen: set[str] = set()
    unique = []
    for name in names:
        if name.lower() not in seen:
            seen.add(name.lower())
            unique.append(name)
    return tuple(unique)


def _describe(expression: Expression) -> str:
    match expression:
        case Call(name=name):
            return f"function {name.upper()}"
        case Binary(op=op) if op in ("+", "-", "*", "/", "**"):
            return "arithmetic"
        case Binary(op="||"):
            return "concatenation"
        case Unary(op="-" | "+"):
            return "arithmetic"
        case Name():
            return "a variable"
        case Number() | String() | Missing():
            return "a constant"
    return "an expression"
