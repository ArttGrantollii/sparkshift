"""Every SAS program in examples/sas returns rows written by hand.

As for the SAS scenarios in test_sas.py, no SAS is available to produce a
reference result: the expected rows follow the rules in docs/sas.md.
"""

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from harness import assert_sas_result
from test_sas import Sorted, _result

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

EXAMPLES = Path(__file__).parents[2] / "examples" / "sas"

# The examples' results are sorted, so each has its order keys.
EXPECTED: dict[str, Sorted] = {
    # Missing scores are left out; CATX skips customer 4's missing country
    # and customer 5's empty name; the IF/ELSE chain picks the first match.
    "customer_ratings": Sorted(
        "score, customer_id",
        _result(
            "customer_id int, name string, country string, score double, "
            "label string, rating string",
            (7, "Grace", "FR", 5.0, "Grace, FR", "high"),
            (7, "Grace", "FR", 5.0, "Grace, FR", "high"),
            (1, "Alice", "US", 4.5, "Alice, US", "high"),
            (2, "Bob", "US", 3.0, "Bob, US", "mid"),
            (6, None, "DE", 2.5, "DE", "mid"),
            (4, "Zoë", None, 0.0, "Zoë", "low"),
            (5, "", "DE", -1.25, "DE", "low"),
        ),
    ),
    # Orders 107 and 108 have no matching customer. Customer 4, who placed
    # order 106, has no country: a group of its own.
    "revenue_by_country": Sorted(
        "revenue",
        _result(
            "country string, orders bigint, revenue decimal(20,2)",
            (None, 1, Decimal("999.99")),
            ("US", 5, Decimal("360.50")),
            ("ES", 2, Decimal("-15.75")),
        ),
    ),
    # Order 106's missing status keeps its count, without a percentage.
    "status_counts": Sorted(
        "status",
        _result(
            "status string, COUNT bigint, PERCENT double",
            (None, 1, None),
            ("cancelled", 1, 100 / 9),
            ("completed", 6, 600 / 9),
            ("pending", 1, 100 / 9),
            ("refunded", 1, 100 / 9),
        ),
    ),
}


def test_every_example_has_expected_rows() -> None:
    assert {path.stem for path in EXAMPLES.glob("*.sas")} == set(EXPECTED)


@pytest.mark.spark
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sas_example(spark_tables: "SparkSession", name: str) -> None:
    program = (EXAMPLES / f"{name}.sas").read_text(encoding="utf-8")
    expected = EXPECTED[name]

    keys = expected.keys.split(", ")
    assert_sas_result(spark_tables, program, expected.result, keys)
