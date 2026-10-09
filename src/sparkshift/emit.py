"""Generate PySpark source code from SparkShift's IR.

The emitter only knows the IR and how to write readable Python. It must not
import SQLGlot or PySpark.

Generated code is deterministic: the same IR always produces the same text.
"""

import keyword
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from decimal import Decimal
from typing import TypeAlias, assert_never

from sparkshift import ir

RESULT_VARIABLE = "result"

# What a chain step such as .select(...) or .orderBy(...) takes: expressions,
# sort keys, or code that is already written, such as a table or a number.
_Argument: TypeAlias = ir.Expression | ir.SortKey | str

# Names the generated code already uses; table variables must not shadow them.
_RESERVED_NAMES = frozenset({RESULT_VARIABLE, "spark", "F", "Decimal", "Window"})

_INDENT = "    "
_MAX_LINE_LENGTH = 88
# Chain steps start at column 4 and their arguments at column 8.
_ARGUMENT_COLUMN = 8

# Identifier parts Spark accepts without backtick quoting.
_PLAIN_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Parameters that PySpark functions take as plain Python values rather than
# Columns, by position: F.round(col, 2), F.substring(col, 2, 3),
# F.concat_ws("", ...), F.log(10.0, col), F.lag(col, 2, 0).
_PLAIN_VALUE_PARAMETERS = {
    "round": frozenset({1}),
    "substring": frozenset({1, 2}),
    "concat_ws": frozenset({0}),
    "log": frozenset({0}),
    "date_trunc": frozenset({0}),
    "trunc": frozenset({1}),
    "lag": frozenset({1, 2}),
    "lead": frozenset({1, 2}),
    "ntile": frozenset({0}),
}

# DataFrame methods for set operations, by operator and whether duplicates
# are removed. UNION removes them with an extra .distinct().
_SET_OPERATION_METHODS = {
    (ir.SetOperator.UNION, True): "union",
    (ir.SetOperator.UNION, False): "union",
    (ir.SetOperator.INTERSECT, True): "intersect",
    (ir.SetOperator.INTERSECT, False): "intersectAll",
    (ir.SetOperator.EXCEPT, True): "subtract",
    (ir.SetOperator.EXCEPT, False): "exceptAll",
}

# Flags passed by keyword, so the code says what they mean:
# F.first_value(col, ignoreNulls=True).
_KEYWORD_PARAMETERS = {
    "first_value": {1: "ignoreNulls"},
    "last_value": {1: "ignoreNulls"},
}

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


def emit(plan: ir.Relation, *, line_length: int = _MAX_LINE_LENGTH) -> str:
    """Return PySpark code that assigns the plan's DataFrame to ``result``.

    The code imports what it uses and expects a SparkSession named ``spark``.
    Expressions longer than ``line_length`` are wrapped across lines.
    """
    return _Emitter(line_length).program(plan)


def emit_expression(expression: ir.Expression) -> str:
    """Return the PySpark code for a single expression."""
    return _Emitter().expression(expression)


class _Emitter:
    def __init__(self, line_length: int = _MAX_LINE_LENGTH) -> None:
        self.line_length = line_length
        self.uses_functions = False
        self.uses_decimal = False
        self.uses_window = False
        self.table_variables: dict[tuple[str, ...], str] = {}
        self.table_uses: Counter[tuple[str, ...]] = Counter()
        self.window_variables: dict[_WindowKey, str] = {}
        self.named_variables: dict[ir.Named, str] = {}
        # Variable declarations for named relations, in the order the code
        # needs them, so each comes after the ones it uses.
        self.declarations: list[str] = []
        self.taken_names: set[str] = set(_RESERVED_NAMES)
        # Extra indentation of the chain being rendered, for a chain nested
        # inside a call such as .union(...).
        self.nesting = 0

    def program(self, plan: ir.Relation) -> str:
        self.table_uses = Counter(
            node.name_parts for node in _walk(plan) if isinstance(node, ir.TableScan)
        )
        # Queries that combine tables declare each source table once, up front.
        if any(isinstance(node, ir.Join) for node in _walk(plan)):
            self.table_variables = table_variables(plan)
        # Each distinct window is declared once, so calls can share it.
        self.window_variables = window_variables(
            plan, set(self.table_variables.values())
        )
        self.taken_names |= {*self.table_variables.values()}
        self.taken_names |= {*self.window_variables.values()}

        statement = f"{RESULT_VARIABLE} = {self.relation(plan)}\n"
        windows = [
            self.window_variable(name, key)
            for key, name in self.window_variables.items()
        ]
        imports = []
        if self.uses_window:
            imports.append("from pyspark.sql import Window")
        if self.uses_functions:
            imports.append("from pyspark.sql import functions as F")
        sections = []
        if self.uses_decimal:
            sections.append("from decimal import Decimal")
        if imports:
            sections.append("\n".join(imports))
        if self.table_variables:
            sections.append(
                "\n".join(
                    f"{name} = {_spark_table(parts)}"
                    for parts, name in self.table_variables.items()
                )
            )
        if windows:
            sections.append("\n".join(windows))
        # Standard-library imports, third-party imports, table variables,
        # windows, each named relation, and the result are separated by blank
        # lines.
        return "\n\n".join([*sections, *self.declarations, statement])

    def named_variable(self, node: ir.Named) -> str:
        """The variable holding a named relation. It is declared the first time
        the code uses it, after any named relations it uses itself."""
        name = self.named_variables.get(node)
        if name is None:
            # Declarations are not nested, whatever the code that uses them.
            nesting, self.nesting = self.nesting, 0
            code = self.relation(node.source)
            self.nesting = nesting
            name = _unique_name(_python_name(node.name), self.taken_names)
            self.taken_names.add(name)
            self.named_variables[node] = name
            self.declarations.append(f"{name} = {code}")
        return name

    def window_variable(self, name: str, key: "_WindowKey") -> str:
        """``name = Window.partitionBy(...).orderBy(...)``, on one line if it
        fits, otherwise one call per line."""
        calls = self.window_calls(key)
        inline = f"{name} = Window{''.join(calls)}"
        if "\n" not in inline and len(inline) <= self.line_length:
            return inline
        lines = [f"Window{calls[0]}", *calls[1:]]
        body = "".join(_indent(line) + "\n" for line in lines)
        return f"{name} = (\n{body})"

    def window_calls(self, key: "_WindowKey") -> list[str]:
        self.uses_window = True
        partition_by, order_by, frame = key
        calls = []
        if partition_by or not order_by:
            # An empty partitionBy() is one window over all rows: OVER ().
            calls.append(self.call("partitionBy", partition_by))
        if order_by:
            calls.append(self.call("orderBy", order_by))
        if frame is not None:
            method = "rowsBetween" if frame.rows else "rangeBetween"
            start = _frame_bound(frame.start, "Window.unboundedPreceding")
            end = _frame_bound(frame.end, "Window.unboundedFollowing")
            calls.append(f".{method}({start}, {end})")
        return calls

    def window_name(self, call: ir.WindowCall) -> str:
        """The variable holding a call's window, or the window itself when the
        code is a single expression with no variables."""
        key = _window_key(call)
        name = self.window_variables.get(key)
        return name if name is not None else f"Window{''.join(self.window_calls(key))}"

    def window_function(self, call: ir.WindowCall) -> str:
        if isinstance(call.function, ir.WindowFunction):
            self.uses_functions = True
            return f"F.{call.function.value}()"
        return self.expression(call.function)

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
            case ir.Named():
                return self.named_variable(plan), []
            case ir.RenameColumns(source=source, names=names):
                start, calls = self.chain(source)
                renamed = self.call("toDF", [python_string(name) for name in names])
                return start, [*calls, renamed]
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
                on: ir.Expression | str
                if plan.condition is not None:
                    on = plan.condition
                else:
                    on = "[" + ", ".join(python_string(c) for c in plan.using) + "]"
                arguments = [other, on, python_string(kind.value)]
                return start, [*calls, self.call("join", arguments)]
            case ir.Filter(source=source, condition=condition):
                start, calls = self.chain(source)
                return start, [*calls, self.call("where", [condition])]
            case ir.Aggregate(source=source, keys=keys, aggregates=aggregates):
                start, calls = self.chain(source)
                if not aggregates:
                    # Grouping without aggregates is the distinct key values.
                    steps = [self.call("select", keys), self.call("distinct", [])]
                elif not keys:
                    steps = [self.call("agg", aggregates)]
                else:
                    steps = [self.call("groupBy", keys), self.call("agg", aggregates)]
                return start, [*calls, *steps]
            case ir.Project(source=source, items=items):
                start, calls = self.chain(source)
                return start, [*calls, self.call("select", items)]
            case ir.SetOperation(
                operator=operator, left=left, right=right, distinct=distinct
            ):
                start, calls = self.chain(left)
                method = _SET_OPERATION_METHODS[operator, distinct]
                steps = [self.call(method, [self.nested_relation(right)])]
                if operator is ir.SetOperator.UNION and distinct:
                    # PySpark's union keeps duplicates, like UNION ALL.
                    steps.append(self.call("distinct", []))
                return start, [*calls, *steps]
            case ir.Distinct(source=source):
                start, calls = self.chain(source)
                return start, [*calls, self.call("distinct", [])]
            case ir.Sort(source=source, keys=keys):
                start, calls = self.chain(source)
                return start, [*calls, self.call("orderBy", keys)]
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

    def nested_relation(self, plan: ir.Relation) -> str:
        """A relation used as an argument, as a chain with one call per line
        (indented by the call it is passed to), or a single line if it has no
        calls."""
        self.nesting += len(_INDENT)
        try:
            start, calls = self.chain(plan)
        finally:
            self.nesting -= len(_INDENT)
        return "\n".join([start, *calls])

    def inline_relation(self, plan: ir.Relation) -> str:
        """Render a relation used as an argument, such as the right side of a join."""
        start, calls = self.chain(plan)
        return start + "".join(calls)

    def call(self, method: str, arguments: Sequence[_Argument]) -> str:
        """Render a chain step such as ``.select(...)``: inline if it is short
        and has at most one argument; otherwise each argument on its own line,
        wrapped further if it is still too long."""
        one_line = [self.code(argument) for argument in arguments]
        inline = f".{method}({', '.join(one_line)})"
        step_column = len(_INDENT) + self.nesting
        if (
            len(arguments) <= 1
            and "\n" not in inline
            and step_column + len(inline) <= self.line_length
        ):
            return inline
        column = _ARGUMENT_COLUMN + self.nesting
        codes = [self.argument(argument, column) for argument in arguments]
        if len(codes) == 1:
            return f".{method}(\n{_indent(codes[0])}\n)"
        body = "".join(_indent(f"{code},") + "\n" for code in codes)
        return f".{method}(\n{body})"

    # --- Layout of long expressions ------------------------------------------
    #
    # Wrapping only changes line breaks and grouping parentheses, never the
    # expression: tests check that wrapped and one-line code parse to the same
    # Python syntax tree. Continuation lines are indented relative to the
    # expression's first line; ``column`` is where that first line starts.

    def code(self, value: _Argument) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, ir.SortKey):
            return f"{self.operand(value.expression, _ATOM)}{_sort_suffix(value)}"
        return self.expression(value)

    def argument(self, value: _Argument, column: int) -> str:
        """Code for a value starting at ``column``, wrapped if it does not fit
        (leaving room for a trailing comma)."""
        code = self.code(value)
        if isinstance(value, str) or column + len(code) + 1 <= self.line_length:
            return code
        if isinstance(value, ir.SortKey):
            suffix = _sort_suffix(value)
            return self.suffixed(value.expression, suffix, column) or code
        return self.wrapped(value, column) or code

    def wrapped(self, expression: ir.Expression, column: int) -> str | None:
        """Multi-line code for an expression, or None if it has no good break."""
        match expression:
            case ir.Alias(expression=inner, name=name):
                return self.suffixed(inner, f".alias({python_string(name)})", column)
            case ir.BinaryOp(op=op) if op in _BOOLEAN_OPERATORS:
                return self.boolean_chain(expression, column)
            case ir.UnaryOp(op=op, operand=operand):
                if self.render(operand)[1] < _ATOM:
                    return f"{op.value}{self.parenthesized(operand, column)}"
                inner_code = self.wrapped(operand, column)
                return None if inner_code is None else f"{op.value}{inner_code}"
            case ir.Case(branches=branches, default=default):
                self.uses_functions = True
                steps = [
                    self.call_text(
                        "F.when" if i == 0 else ".when", [when, then], column
                    )
                    for i, (when, then) in enumerate(branches)
                ]
                if default is not None:
                    steps.append(self.call_text(".otherwise", [default], column))
                return "\n".join(steps)
            case ir.FunctionCall(name=name, arguments=arguments):
                self.uses_functions = True
                values = self.function_arguments(name, arguments)
                return self.wrapped_call(f"F.{name}", values, column)
            case ir.AggregateCall(arguments=arguments) if arguments:
                code = self.expression(expression)
                return self.wrapped_call(
                    code[: code.index("(")], list(arguments), column
                )
            case ir.InList(expression=inner, values=values):
                receiver = f"{self.operand(inner, _ATOM)}.isin"
                return self.wrapped_call(receiver, list(values), column)
            case ir.WindowCall(
                function=ir.AggregateCall() | ir.FunctionCall() as function
            ):
                suffix = f".over({self.window_name(expression)})"
                return self.suffixed(function, suffix, column)
            case ir.Between(expression=inner, low=low, high=high):
                receiver = f"{self.operand(inner, _ATOM)}.between"
                return self.wrapped_call(receiver, [low, high], column)
        return None

    def suffixed(self, inner: ir.Expression, suffix: str, column: int) -> str | None:
        """Multi-line code for ``inner`` followed by a method call such as
        ``.alias("x")`` or ``.desc()``, or None if it has no good break."""
        if self.render(inner)[1] < _ATOM:
            # Already needs parentheses before the method: give it its own
            # lines inside them, as Black does.
            return f"{self.parenthesized(inner, column)}{suffix}"
        inner_code = self.wrapped(inner, column)
        if inner_code is None:
            return None
        separator = "\n" if isinstance(inner, ir.Case) else ""
        return f"{inner_code}{separator}{suffix}"

    def call_text(
        self, prefix: str, arguments: Sequence[ir.Expression | str], column: int
    ) -> str:
        """A call on one line if it fits, otherwise one argument per line."""
        inline = f"{prefix}({', '.join(self.code(argument) for argument in arguments)})"
        if column + len(inline) <= self.line_length:
            return inline
        return self.wrapped_call(prefix, arguments, column)

    def wrapped_call(
        self, prefix: str, arguments: Sequence[ir.Expression | str], column: int
    ) -> str:
        inner = column + len(_INDENT)
        body = "".join(
            _indent(f"{self.argument(argument, inner)},") + "\n"
            for argument in arguments
        )
        return f"{prefix}(\n{body})"

    def boolean_chain(self, expression: ir.BinaryOp, column: int) -> str:
        """``a & b & c`` as one condition per line, operators leading."""
        op = expression.op
        operands = _flatten_left(expression)
        first_min, rest_min = _boolean_operand_minimums(op)
        lines = []
        for position, operand in enumerate(operands):
            minimum = first_min if position == 0 else rest_min
            text = self.boolean_operand(operand, minimum, column)
            lines.append(text if position == 0 else f"{op.value} {text}")
        return "\n".join(lines)

    def parenthesized(self, expression: ir.Expression, column: int) -> str:
        """An expression in its own parentheses: on one line inside them if
        that fits, otherwise wrapped further (a boolean chain gets one
        condition per line)."""
        inner = column + len(_INDENT)
        body = self.expression(expression)
        if inner + len(body) > self.line_length:
            body = self.wrapped(expression, inner) or body
        return f"(\n{_indent(body)}\n)"

    def boolean_operand(self, operand: ir.Expression, minimum: int, column: int) -> str:
        code = self.operand(operand, minimum)
        if column + len(code) + 2 <= self.line_length:
            return code
        _, precedence = self.render(operand)
        if (
            precedence < minimum
            and isinstance(operand, ir.BinaryOp)
            and operand.op in _BOOLEAN_OPERATORS
        ):
            return self.parenthesized(operand, column)
        if precedence >= minimum:
            return self.wrapped(operand, column) or code
        return code

    def function_arguments(
        self, name: str, arguments: tuple[ir.Expression, ...]
    ) -> list[ir.Expression | str]:
        """Arguments of a pyspark function, with plain-value parameters rendered
        as Python values: F.round(col, 2), not F.round(col, F.lit(2))."""
        plain = _PLAIN_VALUE_PARAMETERS.get(name, frozenset())
        keywords = _KEYWORD_PARAMETERS.get(name, {})
        values: list[ir.Expression | str] = []
        for position, argument in enumerate(arguments):
            if position in keywords and isinstance(argument, ir.Literal):
                value = self.python_value(argument.value)
                values.append(f"{keywords[position]}={value}")
            elif position in plain and isinstance(argument, ir.Literal):
                values.append(self.python_value(argument.value))
            else:
                values.append(argument)
        return values

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
            case ir.AggregateCall(
                function=function, arguments=arguments, distinct=distinct
            ):
                self.uses_functions = True
                if not arguments:
                    # COUNT(*): count rows, whatever their values.
                    return "F.count(F.lit(1))", _ATOM
                name = function.value
                if distinct:
                    name = f"{name}_distinct"
                code = ", ".join(self.expression(argument) for argument in arguments)
                return f"F.{name}({code})", _ATOM
            case ir.InList(expression=inner, values=values):
                code = ", ".join(self.expression(value) for value in values)
                return self.method(inner, "isin", code), _ATOM
            case ir.Between(expression=inner, low=low, high=high):
                bounds = f"{self.expression(low)}, {self.expression(high)}"
                return self.method(inner, "between", bounds), _ATOM
            case ir.Like(
                expression=inner, pattern=pattern, case_insensitive=insensitive
            ):
                name = "ilike" if insensitive else "like"
                return self.method(inner, name, python_string(pattern)), _ATOM
            case ir.IsNull(expression=inner, negated=negated):
                name = "isNotNull" if negated else "isNull"
                return self.method(inner, name, ""), _ATOM
            case ir.NullSafeEqual(left=left, right=right):
                return self.method(left, "eqNullSafe", self.expression(right)), _ATOM
            case ir.Case(branches=branches, default=default):
                self.uses_functions = True
                (first_when, first_then), *rest = branches
                when_code = self.expression(first_when)
                code = f"F.when({when_code}, {self.expression(first_then)})"
                for when, then in rest:
                    code += f".when({self.expression(when)}, {self.expression(then)})"
                if default is not None:
                    code += f".otherwise({self.expression(default)})"
                return code, _ATOM
            case ir.FunctionCall(name=name, arguments=arguments):
                self.uses_functions = True
                values = self.function_arguments(name, arguments)
                code = ", ".join(self.code(value) for value in values)
                return f"F.{name}({code})", _ATOM
            case ir.Interval(unit=unit, amount=amount):
                # make_interval keeps the input type when added: a date stays
                # a date, and a timestamp keeps its time of day.
                self.uses_functions = True
                return f"F.make_interval({unit}={self.expression(amount)})", _ATOM
            case ir.Cast(expression=inner, data_type=data_type, safe=safe):
                name = "try_cast" if safe else "cast"
                return self.method(inner, name, python_string(data_type)), _ATOM
            case ir.WindowCall():
                window = self.window_name(expression)
                return f"{self.window_function(expression)}.over({window})", _ATOM
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

    def method(self, receiver: ir.Expression, name: str, arguments: str) -> str:
        """Render a Column method call, such as ``F.col("a").isin(...)``."""
        return f"{self.operand(receiver, _ATOM)}.{name}({arguments})"

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
    """Yield every relation in the plan, left side before right side. A named
    relation used several times is computed once, so its body is visited
    once."""
    seen: set[ir.Named] = set()

    def walk(node: ir.Relation) -> Iterator[ir.Relation]:
        yield node
        match node:
            case ir.TableScan():
                return
            case ir.Named(source=source):
                if node not in seen:
                    seen.add(node)
                    yield from walk(source)
            case (
                ir.Join(left=left, right=right)
                | ir.SetOperation(left=left, right=right)
            ):
                yield from walk(left)
                yield from walk(right)
            case (
                ir.RenameColumns(source=source)
                | ir.RelationAlias(source=source)
                | ir.Filter(source=source)
                | ir.Aggregate(source=source)
                | ir.Project(source=source)
                | ir.Distinct(source=source)
                | ir.Sort(source=source)
                | ir.Limit(source=source)
            ):
                yield from walk(source)

    return walk(plan)


def _unique_name(name: str, taken: set[str]) -> str:
    """``name``, or ``name_2``, ``name_3``, ... if it is already taken."""
    if name not in taken:
        return name
    suffix = 2
    while f"{name}_{suffix}" in taken:
        suffix += 1
    return f"{name}_{suffix}"


_WindowKey: TypeAlias = tuple[
    tuple[ir.Expression, ...], tuple[ir.SortKey, ...], ir.WindowFrame | None
]


def _window_key(call: ir.WindowCall) -> _WindowKey:
    """A window's identity: its PARTITION BY, ORDER BY, and frame."""
    return call.partition_by, call.order_by, call.frame


def _frame_bound(offset: int | None, unbounded: str) -> str:
    if offset is None:
        return unbounded
    return "Window.currentRow" if offset == 0 else str(offset)


def window_variables(plan: ir.Relation, taken: set[str]) -> dict[_WindowKey, str]:
    """Name each distinct window in the order the code uses them: ``window``
    if there is one, ``window_1``, ``window_2``, ... if there are several.
    Names already ``taken`` by table variables get a ``_spec`` suffix."""
    keys: list[_WindowKey] = []
    for expression in _expressions(plan):
        if isinstance(expression, ir.WindowCall):
            key = _window_key(expression)
            if key not in keys:
                keys.append(key)
    names = (
        ["window"] if len(keys) == 1 else [f"window_{n + 1}" for n in range(len(keys))]
    )
    return {
        key: f"{name}_spec" if name in taken else name
        for key, name in zip(keys, names, strict=True)
    }


def _expressions(plan: ir.Relation) -> Iterator[ir.Expression]:
    """Yield every expression in the plan and all their sub-expressions, from
    the source relation up, as the generated chain uses them."""
    for node in reversed(list(_walk(plan))):
        roots: Sequence[ir.Expression] = ()
        match node:
            case ir.Join(condition=condition) if condition is not None:
                roots = (condition,)
            case ir.Filter(condition=condition):
                roots = (condition,)
            case ir.Aggregate(keys=keys, aggregates=aggregates):
                roots = (*keys, *aggregates)
            case ir.Project(items=items):
                roots = items
            case ir.Sort(keys=sort_keys):
                roots = tuple(key.expression for key in sort_keys)
        for root in roots:
            yield from _subexpressions(root)


def _subexpressions(expression: ir.Expression) -> Iterator[ir.Expression]:
    yield expression
    for child in ir.children(expression):
        yield from _subexpressions(child)


def _sort_suffix(key: ir.SortKey) -> str:
    """The Column method for a sort key. Spark puts NULLs first when ascending
    and last when descending; other placements are spelled out."""
    if key.descending:
        return ".desc_nulls_first()" if key.nulls_first else ".desc()"
    return ".asc()" if key.nulls_first else ".asc_nulls_last()"


def _spark_table(parts: tuple[str, ...]) -> str:
    return f"spark.table({python_string(spark_identifier(parts))})"


_BOOLEAN_OPERATORS = frozenset({ir.BinaryOperator.AND, ir.BinaryOperator.OR})


def _flatten_left(expression: ir.BinaryOp) -> list[ir.Expression]:
    """Turn ((a & b) & c) into [a, b, c]. Only the left side is flattened, so
    the operands keep the tree's left-to-right grouping."""
    operands = []
    node: ir.Expression = expression
    while isinstance(node, ir.BinaryOp) and node.op is expression.op:
        operands.append(node.right)
        node = node.left
    operands.append(node)
    return operands[::-1]


def _boolean_operand_minimums(op: ir.BinaryOperator) -> tuple[int, int]:
    """The precedence each operand of a boolean chain needs to avoid
    parentheses, matching ``_Emitter.render``: the first operand, then the rest."""
    if op is ir.BinaryOperator.OR:
        return _AND + 1, _AND + 1
    return _AND, _AND + 1
