"""Every SAS program in examples/sas returns rows written by hand.

As for the SAS scenarios in test_sas.py, no SAS is available to produce a
reference result: the expected rows follow the rules in docs/sas.md.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from compare import Snapshot
from harness import assert_sas_result
from test_sas import Sorted, _result

import sparkshift

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

EXAMPLES = Path(__file__).parents[2] / "examples" / "sas"

# The examples' rows; a sorted result has its order keys.
EXPECTED: dict[str, Sorted | Snapshot] = {
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
    # Data lines read by list input; the cleaning step's new text variables
    # take the length of their first value, so "UNDER_30" is stored as
    # "UNDER_3" and "EUROPE" as "EUROP"; PROC SORT orders by country, then
    # customer.
    "customer_cleanup": Sorted(
        "country, customer_id",
        _result(
            "customer_id double, name string, country string, email string, "
            "signup_date date, age double, customer_status string, "
            "age_group string, signup_year int, signup_month int, region string",
            (
                105.0,
                "David",
                "FRANCE",
                "david@test.com",
                date(2024, 5, 12),
                45.0,
                "ADULT",
                "30_PLUS",
                2024,
                5,
                "EUROP",
            ),
            (
                104.0,
                "Emma",
                "GERMANY",
                "emma@test.com",
                date(2024, 4, 5),
                17.0,
                "MINOR",
                "UNDER_3",
                2024,
                4,
                "EUROP",
            ),
            (
                108.0,
                "Anna",
                "GERMANY",
                "anna@test.com",
                date(2024, 8, 10),
                35.0,
                "ADULT",
                "30_PLUS",
                2024,
                8,
                "EUROP",
            ),
            (
                102.0,
                "Sarah",
                "UK",
                "sarah@test.com",
                date(2024, 2, 15),
                32.0,
                "ADULT",
                "30_PLUS",
                2024,
                2,
                "ANGLO",
            ),
            (
                107.0,
                "James",
                "UK",
                "james@test.com",
                date(2024, 7, 1),
                16.0,
                "MINOR",
                "UNDER_3",
                2024,
                7,
                "ANGLO",
            ),
            (
                101.0,
                "John",
                "USA",
                "john@test.com",
                date(2024, 1, 1),
                25.0,
                "ADULT",
                "UNDER_3",
                2024,
                1,
                "ANGLO",
            ),
            (
                103.0,
                "Michael",
                "USA",
                "michael@test.com",
                date(2024, 3, 20),
                0.0,
                "MINOR",
                "UNDER_3",
                2024,
                3,
                "ANGLO",
            ),
            (
                106.0,
                "Lisa",
                "USA",
                "lisa@test.com",
                date(2024, 6, 15),
                29.0,
                "ADULT",
                "UNDER_3",
                2024,
                6,
                "ANGLO",
            ),
        ),
    ),
    # Each account's transactions summed by type with CASE; CALCULATED
    # columns computed from the sums; then the risk level and VIP flag.
    "account_risk": _result(
        "account_id double, customer_id double, account_type string, "
        "balance double, transaction_count bigint, total_deposits double, "
        "total_withdrawals double, net_change double, final_balance double, "
        "risk_level string, vip_flag int",
        (
            1001.0,
            101.0,
            "SAVINGS",
            15000.0,
            3,
            1700.0,
            200.0,
            1500.0,
            16500.0,
            "MEDIUM",
            0,
        ),
        (
            1002.0,
            102.0,
            "CHECKING",
            25000.0,
            2,
            1500.0,
            300.0,
            1200.0,
            26200.0,
            "MEDIUM",
            0,
        ),
        (1003.0, 103.0, "SAVINGS", 8000.0, 1, 700.0, 0.0, 700.0, 8700.0, "HIGH", 0),
        (
            1004.0,
            104.0,
            "BUSINESS",
            120000.0,
            2,
            10000.0,
            5000.0,
            5000.0,
            125000.0,
            "LOW",
            1,
        ),
        (1005.0, 105.0, "CHECKING", 45000.0, 2, 250.0, 100.0, 150.0, 45150.0, "LOW", 0),
    ),
}

# Each example's warnings: (message, source fragment).
WARNINGS: dict[str, list[tuple[str, str]]] = {
    "account_risk": [],
    "customer_cleanup": [
        ("text truncated to the length of age_group", 'age_group = "UNDER_30"'),
        ("text truncated to the length of region", 'region = "EUROPE"'),
    ],
    "customer_ratings": [
        ("KEEP of several variables", "keep=customer_id name country score"),
    ],
    "revenue_by_country": [
        ("equality of two columns", "o.customer_id = c.customer_id"),
    ],
    "status_counts": [],
}


def test_every_example_has_expected_rows_and_warnings() -> None:
    names = {path.stem for path in EXAMPLES.glob("*.sas")}
    assert names == set(EXPECTED) == set(WARNINGS)


@pytest.mark.parametrize("name", sorted(WARNINGS))
def test_sas_example_warnings(name: str) -> None:
    program = (EXAMPLES / f"{name}.sas").read_text(encoding="utf-8")

    warnings = sparkshift.convert(program, dialect="sas").warnings

    assert [(warning.message, warning.sql) for warning in warnings] == WARNINGS[name]


@pytest.mark.spark
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sas_example(spark_tables: "SparkSession", name: str) -> None:
    program = (EXAMPLES / f"{name}.sas").read_text(encoding="utf-8")
    expected = EXPECTED[name]

    if isinstance(expected, Sorted):
        keys = expected.keys.split(", ")
        assert_sas_result(spark_tables, program, expected.result, keys)
    else:
        assert_sas_result(spark_tables, program, expected)
