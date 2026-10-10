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


@dataclass(frozen=True)
class SortKey:
    """One sort key: ascending unless ``descending``, with NULLs placed first
    or last. The NULL placement is always explicit, because databases disagree
    on the default."""

    expression: "Expression"
    descending: bool
    nulls_first: bool


class WindowFunction(Enum):
    """Functions that only exist over a window: they number or rank rows."""

    ROW_NUMBER = "row_number"
    RANK = "rank"
    DENSE_RANK = "dense_rank"


@dataclass(frozen=True)
class WindowFrame:
    """The rows of a window that a function covers, relative to the current
    row: ``start`` and ``end`` are offsets, negative before the current row
    and positive after it, or None for the window's first or last row.

    With ``rows``, offsets count rows (SQL's ROWS). Otherwise rows that tie
    with the current row on the window's ORDER BY count as one (RANGE), and
    only None and 0 are allowed.
    """

    rows: bool
    start: int | None
    end: int | None

    def __post_init__(self) -> None:
        if not self.rows and {self.start, self.end} - {None, 0}:
            raise ValueError("A RANGE frame only takes unbounded or 0 offsets")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("A window frame cannot end before it starts")


@dataclass(frozen=True)
class WindowCall:
    """A function computed for each row over related rows, as in
    ``SUM(amount) OVER (PARTITION BY customer_id ORDER BY order_date)``.

    Rows with the same ``partition_by`` values form a window. Without a
    ``frame``, a function with ``order_by`` covers the rows up to the current
    one, including all rows that tie with it (SQL's default frame); without
    ``order_by``, the whole window. ``function`` is a ranking, an aggregate,
    or a function that only works over a window, such as ``lag``.
    """

    function: "WindowFunction | AggregateCall | FunctionCall"
    partition_by: tuple["Expression", ...] = ()
    order_by: tuple[SortKey, ...] = ()
    frame: WindowFrame | None = None


@dataclass(frozen=True)
class OuterColumn:
    """A column of the enclosing query, used inside a correlated subquery, as
    ``c.customer_id`` in ``EXISTS (... WHERE o.customer_id = c.customer_id)``.
    It holds name parts, not a Column, so rewrites of the subquery's own
    columns never touch it."""

    name_parts: tuple[str, ...]


@dataclass(frozen=True)
class InSubquery:
    """``expression IN (subquery)``: true if the subquery's single column has
    an equal value; NULL rather than false when it has none but holds a
    NULL."""

    expression: "Expression"
    query: "Relation"


@dataclass(frozen=True)
class Exists:
    """``EXISTS (subquery)``: whether the subquery returns any row. Never NULL."""

    query: "Relation"


@dataclass(frozen=True)
class ScalarSubquery:
    """A subquery that returns one column and at most one row, used as a
    value; NULL when it returns no row."""

    query: "Relation"


Expression: TypeAlias = (
    Column
    | OuterColumn
    | InSubquery
    | Exists
    | ScalarSubquery
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
    | WindowCall
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
    if isinstance(value, SortKey):
        return replace(value, expression=function(value.expression))
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
class Named:
    """A relation the query gives a name, as a CTE (``WITH name AS (...)``) or
    a subquery in FROM (``(SELECT ...) AS name``) does. It is computed once
    and can be used several times; equal Named nodes are the same relation."""

    name: str
    source: "Relation"


@dataclass(frozen=True)
class RenameColumns:
    """Give the source's columns new names, by position, as a CTE's or a
    subquery's column list does: ``WITH t (a, b) AS (...)``."""

    source: "Relation"
    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.names:
            raise ValueError("RenameColumns needs at least one name")


@dataclass(frozen=True)
class DropColumns:
    """Remove columns by name, such as helper columns a filter needed."""

    source: "Relation"
    names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.names:
            raise ValueError("DropColumns needs at least one name")


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


class SetOperator(Enum):
    UNION = "union"
    INTERSECT = "intersect"
    EXCEPT = "except"


@dataclass(frozen=True)
class SetOperation:
    """Combine the rows of two relations with the same number of columns,
    matched by position; the output takes the left relation's column names.

    With ``distinct``, duplicate rows are removed from the result (SQL's
    default); otherwise duplicates count, as with ``UNION ALL``. NULLs compare
    as equal.
    """

    operator: SetOperator
    left: "Relation"
    right: "Relation"
    distinct: bool


@dataclass(frozen=True)
class Distinct:
    """Remove duplicate rows; NULLs compare as equal, as in SQL's DISTINCT."""

    source: "Relation"


@dataclass(frozen=True)
class Sort:
    """Order rows by ``keys``, the first key deciding first. Rows equal on
    every key may come in any order."""

    source: "Relation"
    keys: tuple[SortKey, ...]

    def __post_init__(self) -> None:
        if not self.keys:
            raise ValueError("Sort needs at least one key")


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
    TableScan
    | Named
    | RenameColumns
    | DropColumns
    | RelationAlias
    | Join
    | Filter
    | Aggregate
    | Project
    | SetOperation
    | Distinct
    | Sort
    | Limit
)
