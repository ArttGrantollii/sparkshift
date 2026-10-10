"""SAS scenarios: SAS programs whose generated PySpark must return rows
written by hand.

No SAS is available to produce a reference result, so each expected result
is derived by hand from the test data and the documented SAS rule the
scenario names (see docs/sas.md for the rules and their sources). Values
pass through unchanged: a NULL text value stays NULL, where SAS would show a
blank.

Relevant data: customer 3 has a missing score and customer 4 a missing
country; customer 5's name is empty and customer 6's missing; customer 7 is
a duplicate row. Order 105 has a missing amount, order 106 a missing status,
order 107 a missing customer. Product 1's name has leading and trailing
blanks, product 4's trailing blanks only.
"""

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from compare import Snapshot
from harness import assert_sas_result

if TYPE_CHECKING:
    from pyspark.sql import SparkSession

pytestmark = pytest.mark.spark


def _result(columns: str, *rows: tuple[object, ...]) -> Snapshot:
    """Expected columns, written "name type, name type", and rows."""
    pairs = [column.split() for column in columns.split(", ")]
    return Snapshot(tuple((name, kind) for name, kind in pairs), rows)


SAS_SCENARIOS = {
    # A missing numeric value is smaller than any other numeric value.
    "a missing number is smaller than every number": (
        "data low; set customers(keep=customer_id score); where score < 3; run;",
        _result(
            "customer_id int, score double", (3, None), (4, 0.0), (5, -1.25), (6, 2.5)
        ),
    ),
    # A comparison is 1 or 0, never missing, so NOT of it is defined too.
    "NOT of a comparison with a missing value": (
        "data low; set customers(keep=customer_id); where not (score >= 3); run;",
        _result("customer_id int", (3,), (4,), (5,), (6,)),
    ),
    "comparison with the missing value": (
        "data scored; set customers(keep=customer_id); where score ne .; run;",
        _result("customer_id int", (1,), (2,), (4,), (5,), (6,), (7,), (7,)),
    ),
    # Nothing is smaller than the missing value, so every row qualifies.
    "every value is at least the missing value": (
        "data everyone; set customers(keep=customer_id); where score >= .; run;",
        _result("customer_id int", (1,), (2,), (3,), (4,), (5,), (6,), (7,), (7,)),
    ),
    # A missing text value is a blank, so it equals ' '.
    "missing text is blank": (
        "data unnamed; set customers(keep=customer_id name); where name = ' '; run;",
        _result("customer_id int, name string", (5, ""), (6, None)),
    ),
    "not equal includes missing text": (
        "data abroad; set customers(keep=customer_id country); "
        "where country ne 'US'; run;",
        _result(
            "customer_id int, country string",
            (3, "ES"),
            (4, None),
            (5, "DE"),
            (6, "DE"),
            (7, "FR"),
            (7, "FR"),
        ),
    ),
    # Trailing blanks do not count in a comparison; leading blanks do.
    "trailing blanks do not count, leading blanks do": (
        "data found; set products(keep=product_id name); "
        "where name in ('Bolt', 'Widget'); run;",
        _result("product_id int, name string", (4, "Bolt   ")),
    ),
    # A blank, or missing text, is smaller than any other printable text.
    "blank and missing text sort first": (
        "data early; set customers(keep=customer_id name); where name < 'B'; run;",
        _result("customer_id int, name string", (1, "Alice"), (5, ""), (6, None)),
    ),
    "IN list with the missing value": (
        "data some; set orders(keep=order_id customer_id); "
        "where customer_id in (2, .); run;",
        _result(
            "order_id int, customer_id int",
            (103, 2),
            (107, None),
            (109, 2),
            (109, 2),
        ),
    ),
    "NOT IN keeps missing values": (
        "data others; set orders(keep=order_id customer_id); "
        "where customer_id not in (1, 2); run;",
        _result(
            "order_id int, customer_id int",
            (104, 3),
            (105, 3),
            (106, 4),
            (107, None),
            (108, 99),
        ),
    ),
    "BETWEEN excludes missing values": (
        "data middle; set orders(keep=order_id amount); "
        "where amount between 0 and 100; run;",
        _result(
            "order_id int, amount decimal(10,2)",
            (102, Decimal("80.00")),
            (103, Decimal("0.00")),
            (107, Decimal("42.00")),
            (108, Decimal("10.00")),
            (109, Decimal("80.00")),
            (109, Decimal("80.00")),
        ),
    ),
    "IS MISSING for numbers and text": (
        "data gaps; set orders(keep=order_id amount status); "
        "where amount is missing or status is missing; drop amount status; run;",
        _result("order_id int", (105,), (106,)),
    ),
    # Input options: WHERE= and KEEP= use the old names; RENAME= comes after.
    "input options use the old names": (
        "data big; set orders(keep=order_id amount where=(amount > 100) "
        "rename=(amount=total)); run;",
        _result(
            "order_id int, total decimal(10,2)",
            (101, Decimal("120.50")),
            (106, Decimal("999.99")),
        ),
    ),
    # Program statements use the names after input renames.
    "the WHERE statement uses the new names": (
        "data big; set orders(rename=(amount=total)); where total > 100; "
        "keep order_id total; run;",
        _result(
            "order_id int, total decimal(10,2)",
            (101, Decimal("120.50")),
            (106, Decimal("999.99")),
        ),
    ),
    # SAS ignores the WHERE statement for a data set with a WHERE= option.
    "WHERE= wins over the WHERE statement": (
        "data done; set orders(keep=order_id status where=(status = 'completed')); "
        "where order_id > 105; run;",
        _result(
            "order_id int, status string",
            (101, "completed"),
            (102, "completed"),
            (107, "completed"),
            (108, "completed"),
            (109, "completed"),
            (109, "completed"),
        ),
    ),
    # DROP and KEEP statements apply before the RENAME statement, so they use
    # the old names.
    "KEEP statement before the RENAME statement": (
        "data people; set customers; keep customer_id name; "
        "rename name = customer_name; where country = 'FR'; run;",
        _result("customer_id int, customer_name string", (7, "Grace"), (7, "Grace")),
    ),
    # Output options apply last: KEEP= before RENAME=, with the old names.
    "output options apply last": (
        "data out(keep=customer_id name rename=(name=who)); set customers; "
        "where customer_id <= 2; run;",
        _result("customer_id int, who string", (1, "Alice"), (2, "Bob")),
    ),
    "a later step reads an earlier data set": (
        "data de; set customers(keep=customer_id country); where country = 'DE'; run; "
        "data de_ids; set de; drop country; run;",
        _result("customer_id int", (5,), (6,)),
    ),
}


@pytest.mark.parametrize(
    ("program", "expected"), SAS_SCENARIOS.values(), ids=SAS_SCENARIOS.keys()
)
def test_sas_scenario(
    spark_tables: "SparkSession", program: str, expected: Snapshot
) -> None:
    assert_sas_result(spark_tables, program, expected)
