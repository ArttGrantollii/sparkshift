"""SparkShift's intermediate representation (IR).

The IR describes which DataFrame operations to perform, independent of the
source language (SQL today, a SAS subset later) and of how the PySpark code is
formatted. Frontends produce IR; the emitter turns it into code.

Operators are defined in PySpark terms (``&``, ``|``, ``~``, ``==``), not in
the syntax of any source language.

This module must not import SQLGlot or PySpark.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TypeAlias

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


@dataclass(frozen=True)
class Star:
    """All columns of the input, as in ``SELECT *``, or of one input table when
    ``qualifier`` is set, as in ``SELECT c.*``."""

    qualifier: tuple[str, ...] = ()


Expression: TypeAlias = Column | Literal | BinaryOp | UnaryOp | Alias | Star


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
    TableScan | RelationAlias | Join | Filter | Project | Distinct | Limit
)
