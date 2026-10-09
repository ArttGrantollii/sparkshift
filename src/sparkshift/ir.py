"""SparkShift's intermediate representation (IR).

The IR describes which DataFrame operations to perform, independent of the
source language (SQL today, a SAS subset later) and of how the PySpark code is
formatted. Frontends produce IR; the emitter turns it into code.

Operators are defined in PySpark terms (``&``, ``|``, ``~``, ``==``), not in
the syntax of any source language.

This module must not import SQLGlot or PySpark.
"""

from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from decimal import Decimal
from enum import Enum
from typing import TypeAlias, get_args

# --- Expressions -------------------------------------------------------------

# Python value types a literal can hold. Decimal keeps exact numeric literals
# such as 1.50 exact; float is only for literals that are doubles in the source.
LiteralValue: TypeAlias = bool | int | Decimal | float | str | None


class BinaryOperator(Enum):
    ADD = "+"
    SUBTRACT = "-"
    MULTIPLY = "*"
    DIVIDE = "/"
    MODULO = "%"
    EQUAL = "=="
    NOT_EQUAL = "!="
    LESS = "<"
    LESS_EQUAL = "<="
    GREATER = ">"
    GREATER_EQUAL = ">="
    AND = "&"
    OR = "|"


class UnaryOperator(Enum):
    NEGATE = "-"
    NOT = "~"


@dataclass(frozen=True)
class Column:
    """A column reference, e.g. ``("customers", "name")`` for ``customers.name``."""

    name_parts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name_parts:
            raise ValueError("Column needs at least one name part")


@dataclass(frozen=True)
class Literal:
    value: LiteralValue


@dataclass(frozen=True)
class BinaryOp:
    op: BinaryOperator
    left: "Expression"
    right: "Expression"


@dataclass(frozen=True)
class UnaryOp:
    op: UnaryOperator
    operand: "Expression"


@dataclass(frozen=True)
class Alias:
    """An expression with an output column name."""

    expression: "Expression"
    name: str


class AggregateFunction(Enum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


@dataclass(frozen=True)
class AggregateCall:
    """An aggregate over the rows of a group, as in ``SUM(amount)``.

    ``COUNT`` with no arguments counts rows, as in ``COUNT(*)``. Every other
    call ignores NULL inputs, and returns NULL when all inputs are NULL
    (``COUNT`` returns 0).
    """

    function: AggregateFunction
    arguments: tuple["Expression", ...] = ()
    distinct: bool = False


@dataclass(frozen=True)
class Star:
    """All columns of the input, as in ``SELECT *``, or of one input table when
    ``qualifier`` is set, as in ``SELECT c.*``."""

    qualifier: tuple[str, ...] = ()


@dataclass(frozen=True)
class InList:
    """``expression IN (values...)``: NULL unless a value matches, when the
    list contains NULL."""

    expression: "Expression"
    values: tuple["Expression", ...]


@dataclass(frozen=True)
class Between:
    """``expression BETWEEN low AND high``, inclusive at both ends."""

    expression: "Expression"
    low: "Expression"
    high: "Expression"


@dataclass(frozen=True)
class Like:
    """A SQL ``LIKE`` match against a constant pattern using ``%`` and ``_``."""

    expression: "Expression"
    pattern: str
    case_insensitive: bool = False


@dataclass(frozen=True)
class IsNull:
    expression: "Expression"
    negated: bool = False


@dataclass(frozen=True)
class NullSafeEqual:
    """Equality that treats two NULLs as equal and never returns NULL, as in
    ``IS NOT DISTINCT FROM`` or ``<=>``."""

    left: "Expression"
    right: "Expression"


@dataclass(frozen=True)
class Case:
    """``CASE WHEN condition THEN value ... ELSE default END``.

    The first branch whose condition is true wins; with no match and no
    default, the result is NULL.
    """

    branches: tuple[tuple["Expression", "Expression"], ...]
    default: "Expression | None" = None

    def __post_init__(self) -> None:
        if not self.branches:
            raise ValueError("Case needs at least one branch")


@dataclass(frozen=True)
class FunctionCall:
    """A call to a PySpark function in ``pyspark.sql.functions``, by name."""

    name: str
    arguments: tuple["Expression", ...]


@dataclass(frozen=True)
class Interval:
    """A calendar interval, such as 3 days or 1 month, to add to a date or
    timestamp. Adding it keeps the input's type: a date stays a date and a
    timestamp keeps its time of day. ``unit`` is ``"days"``, ``"weeks"``,
    ``"months"``, or ``"years"``."""

    unit: str
    amount: "Expression"

    def __post_init__(self) -> None:
        if self.unit not in {"days", "weeks", "months", "years"}:
            raise ValueError(f"Unsupported interval unit: {self.unit}")


@dataclass(frozen=True)
class Cast:
    """Convert to a Spark type, such as ``"int"`` or ``"decimal(10,2)"``.

    A safe cast returns NULL for values that cannot be converted, instead of
    raising an error.
    """

    expression: "Expression"
    data_type: str
    safe: bool = False


Expression: TypeAlias = (
    Column
    | Literal
    | BinaryOp
    | UnaryOp
    | Alias
    | Star
    | AggregateCall
    | InList
    | Between
    | Like
    | IsNull
    | NullSafeEqual
    | Case
    | FunctionCall
    | Interval
    | Cast
)

_EXPRESSION_TYPES = get_args(Expression)


def children(expression: Expression) -> list[Expression]:
    """Return an expression's direct sub-expressions, in field order."""
    found: list[Expression] = []
    map_children(expression, lambda child: found.append(child) or child)
    return found


def map_children(
    expression: Expression, function: Callable[[Expression], Expression]
) -> Expression:
    """Rebuild an expression with ``function`` applied to each direct
    sub-expression. Works for every expression type, including ones added
    later, because it inspects the dataclass fields."""
    changes = {
        field.name: _map_value(getattr(expression, field.name), function)
        for field in fields(expression)
    }
    return replace(expression, **changes)


def _map_value(value: object, function: Callable[[Expression], Expression]) -> object:
    if isinstance(value, _EXPRESSION_TYPES):
        return function(value)  # type: ignore[arg-type]
    if isinstance(value, tuple):
        return tuple(_map_value(item, function) for item in value)
    return value


# --- Relations ---------------------------------------------------------------


@dataclass(frozen=True)
class TableScan:
    """Read a table, e.g. ``("sales", "customers")`` for ``sales.customers``."""

    name_parts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name_parts:
            raise ValueError("TableScan needs at least one name part")


@dataclass(frozen=True)
class RelationAlias:
    """Give a relation a name that qualified columns can refer to, as in
    ``FROM customers c`` followed by ``c.name``."""

    source: "Relation"
    name: str


class JoinKind(Enum):
    INNER = "inner"
    LEFT = "left"
    RIGHT = "right"
    FULL = "full"
    CROSS = "cross"


@dataclass(frozen=True)
class Join:
    """Combine two relations.

    A cross join has neither ``condition`` nor ``using``. Every other kind has
    exactly one: a join condition, or the names of columns that must be equal
    on both sides (SQL's ``USING``), which appear once in the output.
    """

    left: "Relation"
    right: "Relation"
    kind: JoinKind
    condition: Expression | None = None
    using: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        has_condition = self.condition is not None
        has_using = bool(self.using)
        if self.kind is JoinKind.CROSS and (has_condition or has_using):
            raise ValueError("A cross join takes no condition or USING columns")
        if self.kind is not JoinKind.CROSS and has_condition == has_using:
            raise ValueError("A join needs either a condition or USING columns")


@dataclass(frozen=True)
class Filter:
    """Keep only the rows for which ``condition`` is true.

    Rows where the condition is false or NULL are dropped, as in SQL's WHERE.
    """

    source: "Relation"
    condition: Expression


@dataclass(frozen=True)
class Project:
    """Compute output columns from the source, as in a SELECT list."""

    source: "Relation"
    items: tuple[Expression, ...]


@dataclass(frozen=True)
class Aggregate:
    """Group rows by ``keys`` and compute ``aggregates`` for each group.

    The output has the key columns first, then the aggregates, as PySpark's
    ``groupBy(...).agg(...)`` produces. With no keys, the whole input is one
    group, and the output has exactly one row even when the input is empty.
    """

    source: "Relation"
    keys: tuple[Expression, ...]
    aggregates: tuple[Expression, ...]

    def __post_init__(self) -> None:
        if not self.keys and not self.aggregates:
            raise ValueError("Aggregate needs keys or aggregates")


@dataclass(frozen=True)
class Distinct:
    """Remove duplicate rows; NULLs compare as equal, as in SQL's DISTINCT."""

    source: "Relation"


@dataclass(frozen=True)
class Limit:
    """Keep at most ``count`` rows."""

    source: "Relation"
    count: int

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("Limit count must not be negative")


# Any IR node that produces a DataFrame.
Relation: TypeAlias = (
    TableScan | RelationAlias | Join | Filter | Aggregate | Project | Distinct | Limit
)
