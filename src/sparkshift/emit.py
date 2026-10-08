"""Generate PySpark source code from SparkShift's IR.

The emitter only knows the IR and how to write readable Python. It must not
import SQLGlot or PySpark.

Generated code is deterministic: the same IR always produces the same text.
"""

import keyword
import re
from collections import Counter
from collections.abc import Iterator
from decimal import Decimal
from typing import assert_never

from sparkshift import ir

RESULT_VARIABLE = "result"

# Names the generated code already uses; table variables must not shadow them.
_RESERVED_NAMES = frozenset({RESULT_VARIABLE, "spark", "F", "Decimal"})

_INDENT = "    "
_MAX_LINE_LENGTH = 88

# Identifier parts Spark accepts without backtick quoting.
_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Python operator precedence, from loosest to tightest binding. PySpark builds
# expressions with Python operators, so Python's rules decide where parentheses
# are needed — not SQL's. Notably, & and | bind tighter than ==.
_COMPARISON, _OR, _AND, _ADDITIVE, _MULTIPLICATIVE, _UNARY, _ATOM = range(7)

_PRECEDENCE = {
    ir.BinaryOperator.OR: _OR,
    ir.BinaryOperator.AND: _AND,
    ir.BinaryOperator.EQUAL: _COMPARISON,
    ir.BinaryOperator.NOT_EQUAL: _COMPARISON,
    ir.BinaryOperator.LESS: _COMPARISON,
    ir.BinaryOperator.LESS_EQUAL: _COMPARISON,
    ir.BinaryOperator.GREATER: _COMPARISON,
    ir.BinaryOperator.GREATER_EQUAL: _COMPARISON,
    ir.BinaryOperator.ADD: _ADDITIVE,
    ir.BinaryOperator.SUBTRACT: _ADDITIVE,
    ir.BinaryOperator.MULTIPLY: _MULTIPLICATIVE,
    ir.BinaryOperator.DIVIDE: _MULTIPLICATIVE,
    ir.BinaryOperator.MODULO: _MULTIPLICATIVE,
}


def emit(plan: ir.Relation) -> str:
    """Return PySpark code that assigns the plan's DataFrame to ``result``.

    The code imports what it uses and expects a SparkSession named ``spark``.
    """
    return _Emitter().program(plan)


def emit_expression(expression: ir.Expression) -> str:
    """Return the PySpark code for a single expression."""
    return _Emitter().expression(expression)


class _Emitter:
    def __init__(self) -> None:
        self.uses_functions = False
        self.uses_decimal = False
        self.table_variables: dict[tuple[str, ...], str] = {}
        self.table_uses: Counter[tuple[str, ...]] = Counter()

    def program(self, plan: ir.Relation) -> str:
        self.table_uses = Counter(
            node.name_parts for node in _walk(plan) if isinstance(node, ir.TableScan)
        )
        # Queries that combine tables declare each source table once, up front.
        if any(isinstance(node, ir.Join) for node in _walk(plan)):
            self.table_variables = table_variables(plan)

        statement = f"{RESULT_VARIABLE} = {self.relation(plan)}\n"
        sections = []
        if self.uses_decimal:
            sections.append("from decimal import Decimal")
        if self.uses_functions:
            sections.append("from pyspark.sql import functions as F")
        if self.table_variables:
            sections.append(
                "\n".join(
                    f"{name} = {_spark_table(parts)}"
                    for parts, name in self.table_variables.items()
                )
            )
        # Standard-library imports, third-party imports, table variables, and
        # the result are separated by blank lines.
        return "\n\n".join([*sections, statement])

    # --- Relations -------------------------------------------------------

    def relation(self, plan: ir.Relation) -> str:
        """Render a relation as a method chain, one call per line."""
        source, calls = self.chain(plan)
        if not calls:
            return source
        lines = [source, *calls]
        body = "".join(_indent(line) + "\n" for line in lines)
        return f"(\n{body})"

    def chain(self, plan: ir.Relation) -> tuple[str, list[str]]:
        """Return a relation as its starting expression and its method calls."""
        match plan:
            case ir.TableScan(name_parts=parts):
                return self.table_variables.get(parts, _spark_table(parts)), []
            case ir.RelationAlias(source=source, name=name):
                if self.is_redundant_alias(source, name):
                    return self.chain(source)
                start, calls = self.chain(source)
                alias = f".alias({python_string(name)})"
                if not calls:
                    return start + alias, []
                return start, [*calls, alias]
            case ir.Join(left=left, right=right, kind=kind):
                start, calls = self.chain(left)
                other = self.inline_relation(right)
                if kind is ir.JoinKind.CROSS:
                    return start, [*calls, self.call("crossJoin", [other])]
                if plan.condition is not None:
                    on = self.expression(plan.condition)
                else:
                    on = "[" + ", ".join(python_string(c) for c in plan.using) + "]"
                arguments = [other, on, python_string(kind.value)]
                return start, [*calls, self.call("join", arguments)]
            case ir.Filter(source=source, condition=condition):
                start, calls = self.chain(source)
                return start, [*calls, self.call("where", [self.expression(condition)])]
            case ir.Project(source=source, items=items):
                start, calls = self.chain(source)
                arguments = [self.expression(item) for item in items]
                return start, [*calls, self.call("select", arguments)]
            case ir.Distinct(source=source):
                start, calls = self.chain(source)
                return start, [*calls, self.call("distinct", [])]
            case ir.Limit(source=source, count=count):
                start, calls = self.chain(source)
                return start, [*calls, self.call("limit", [str(count)])]
        assert_never(plan)

    def is_redundant_alias(self, source: ir.Relation, name: str) -> bool:
        """A table used once can already be referred to by its own simple name,
        so aliasing it to that name adds nothing. Self-joins and schema-
        qualified tables keep an explicit alias."""
        return (
            isinstance(source, ir.TableScan)
            and source.name_parts == (name,)
            and self.table_uses[source.name_parts] == 1
        )

    def inline_relation(self, plan: ir.Relation) -> str:
        """Render a relation used as an argument, such as the right side of a join."""
        start, calls = self.chain(plan)
        return start + "".join(calls)

    def call(self, method: str, arguments: list[str]) -> str:
        """Render a method call: inline if it is short and has at most one
        argument, otherwise with one argument per line."""
        inline = f".{method}({', '.join(arguments)})"
        fits = len(_INDENT + inline) <= _MAX_LINE_LENGTH
        if len(arguments) <= 1 and fits and "\n" not in inline:
            return inline
        body = "".join(_indent(f"{argument},") + "\n" for argument in arguments)
        return f".{method}(\n{body})"

    # --- Expressions -----------------------------------------------------

    def expression(self, expression: ir.Expression) -> str:
        return self.render(expression)[0]

    def render(self, expression: ir.Expression) -> tuple[str, int]:
        """Return the code for an expression and its Python precedence."""
        match expression:
            case ir.Star(qualifier=()):
                return '"*"', _ATOM
            case ir.Star(qualifier=qualifier):
                self.uses_functions = True
                name = f"{spark_identifier(qualifier)}.*"
                return f"F.col({python_string(name)})", _ATOM
            case ir.Column(name_parts=parts):
                self.uses_functions = True
                return f"F.col({python_string(spark_identifier(parts))})", _ATOM
            case ir.Literal(value=value):
                self.uses_functions = True
                return f"F.lit({self.python_value(value)})", _ATOM
            case ir.Alias(expression=inner, name=name):
                return (
                    f"{self.operand(inner, _ATOM)}.alias({python_string(name)})",
                    _ATOM,
                )
            case ir.UnaryOp(op=op, operand=operand):
                return f"{op.value}{self.operand(operand, _ATOM)}", _UNARY
            case ir.BinaryOp(op=op, left=left, right=right):
                precedence = _PRECEDENCE[op]
                if op is ir.BinaryOperator.OR:
                    # Python would parse "a & b | c" correctly, but explicit
                    # grouping is easier to read.
                    left_min = right_min = _AND + 1
                else:
                    # Comparisons must never chain ("a == b == c" means
                    # something else in Python), and the right operand of an
                    # equal-precedence operator needs grouping to keep the
                    # tree's shape: a - (b - c).
                    left_min = (
                        precedence + 1 if precedence == _COMPARISON else precedence
                    )
                    right_min = precedence + 1
                left_code = self.operand(left, left_min)
                right_code = self.operand(right, right_min)
                return f"{left_code} {op.value} {right_code}", precedence
        assert_never(expression)

    def operand(self, expression: ir.Expression, minimum: int) -> str:
        """Render an operand, parenthesized if it binds looser than ``minimum``."""
        code, precedence = self.render(expression)
        return f"({code})" if precedence < minimum else code

    def python_value(self, value: ir.LiteralValue) -> str:
        if value is None or isinstance(value, bool | int | float):
            return repr(value)
        if isinstance(value, Decimal):
            self.uses_decimal = True
            return f"Decimal({python_string(str(value))})"
        return python_string(value)


def spark_identifier(parts: tuple[str, ...]) -> str:
    """Join name parts into a Spark multipart identifier.

    Parts that are not plain identifiers are wrapped in backticks, with any
    backtick inside doubled, e.g. ``("sales", "my table")`` becomes
    ``sales.`my table```.
    """
    return ".".join(
        part if _PLAIN_IDENTIFIER.fullmatch(part) else f"`{part.replace('`', '``')}`"
        for part in parts
    )


def python_string(value: str) -> str:
    """Return a double-quoted Python string literal for ``value``."""
    escaped = []
    for char in value:
        if char in ('"', "\\"):
            escaped.append("\\" + char)
        elif char.isprintable():
            escaped.append(char)
        else:
            # repr() yields a valid escape such as \n, \t, or \x00.
            escaped.append(repr(char)[1:-1])
    return '"' + "".join(escaped) + '"'


def _indent(text: str) -> str:
    return "\n".join(_INDENT + line if line else line for line in text.split("\n"))


def table_variables(plan: ir.Relation) -> dict[tuple[str, ...], str]:
    """Choose a Python variable name for each distinct table, in the order the
    tables appear.

    Names come from the table name (``sales.customers`` becomes ``customers``),
    made into valid identifiers that do not shadow names the generated code
    uses. Different tables that would get the same name use their full name.
    """
    tables: list[tuple[str, ...]] = []
    for node in _walk(plan):
        if isinstance(node, ir.TableScan) and node.name_parts not in tables:
            tables.append(node.name_parts)

    names: dict[tuple[str, ...], str] = {}
    for parts in tables:
        candidates = [_python_name(parts[-1]), _python_name("_".join(parts))]
        name = next((c for c in candidates if c not in names.values()), None)
        if name is None:
            suffix = 2
            while f"{candidates[-1]}_{suffix}" in names.values():
                suffix += 1
            name = f"{candidates[-1]}_{suffix}"
        names[parts] = name
    return names


def _python_name(text: str) -> str:
    name = re.sub(r"\W", "_", text)
    if not name or name[0].isdigit():
        name = f"t_{name}"
    if keyword.iskeyword(name) or name in _RESERVED_NAMES:
        name = f"{name}_df"
    return name


def _walk(plan: ir.Relation) -> Iterator[ir.Relation]:
    """Yield every relation in the plan, left side before right side."""
    yield plan
    match plan:
        case ir.TableScan():
            return
        case ir.Join(left=left, right=right):
            yield from _walk(left)
            yield from _walk(right)
        case (
            ir.RelationAlias(source=source)
            | ir.Filter(source=source)
            | ir.Project(source=source)
            | ir.Distinct(source=source)
            | ir.Limit(source=source)
        ):
            yield from _walk(source)


def _spark_table(parts: tuple[str, ...]) -> str:
    return f"spark.table({python_string(spark_identifier(parts))})"
