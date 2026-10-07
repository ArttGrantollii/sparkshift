"""SparkShift's intermediate representation (IR).

The IR describes which DataFrame operations to perform, independent of the
source language (SQL today, a SAS subset later) and of how the PySpark code is
formatted. Frontends produce IR; the emitter turns it into code.

This module must not import SQLGlot or PySpark.
"""

from dataclasses import dataclass
from typing import TypeAlias


@dataclass(frozen=True)
class TableScan:
    """Read a table, e.g. ``("sales", "customers")`` for ``sales.customers``."""

    name_parts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name_parts:
            raise ValueError("TableScan needs at least one name part")


# Any IR node that produces a DataFrame. Grows into a union as features land.
Relation: TypeAlias = TableScan
