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
    """All columns of the input, as in ``SELECT *``."""


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
class Project:
    """Compute output columns from the source, as in a SELECT list."""

    source: "Relation"
    items: tuple[Expression, ...]


# Any IR node that produces a DataFrame.
Relation: TypeAlias = TableScan | Project
