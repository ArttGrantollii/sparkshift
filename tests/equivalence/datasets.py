"""Tables shared by the equivalence tests.

Each table has an explicit schema, so column types never depend on Spark's
inference. Rows are chosen to exercise SQL semantics, not just happy paths;
comments name the edge case each row exists for. Change them with care:
removing an edge case silently weakens every test that uses the table.

The row tables are excluded from auto-formatting so each row stays on one line.
"""

from datetime import date, datetime
from decimal import Decimal

from pyspark.sql.types import (
    BooleanType,
    DateType,
    DecimalType,
    DoubleType,
    IntegerType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

CUSTOMERS_SCHEMA = StructType(
    [
        StructField("customer_id", IntegerType()),
        StructField("name", StringType()),
        StructField("country", StringType()),
        StructField("signup_date", DateType()),
        StructField("is_active", BooleanType()),
        StructField("score", DoubleType()),
    ]
)

# fmt: off
CUSTOMERS = [
    (1, "Alice", "US", date(2023, 1, 15), True,  4.5),
    (2, "Bob",   "US", date(2023, 2, 20), False, 3.0),
    (3, "José",  "ES", date(2023, 3, 5),  True,  None),   # non-ASCII text; NULL double
    (4, "Zoë",   None, None,              None,  0.0),    # NULL string, date, boolean; zero
    (5, "",      "DE", date(2023, 5, 10), True,  -1.25),  # empty string is not NULL
    (6, None,    "DE", date(2023, 6, 1),  True,  2.5),    # NULL name
    (7, "Grace", "FR", date(2023, 7, 4),  False, 5.0),    # has no orders
    (7, "Grace", "FR", date(2023, 7, 4),  False, 5.0),    # exact duplicate row
]
# fmt: on

ORDERS_SCHEMA = StructType(
    [
        StructField("order_id", IntegerType()),
        StructField("customer_id", IntegerType()),
        StructField("amount", DecimalType(10, 2)),
        StructField("status", StringType()),
        StructField("order_date", DateType()),
        StructField("created_at", TimestampType()),
        StructField("discount", DoubleType()),
    ]
)

# fmt: off
ORDERS = [
    (101, 1,    Decimal("120.50"), "completed", date(2024, 1, 3),  datetime(2024, 1, 3, 9, 15),               0.0),
    (102, 1,    Decimal("80.00"),  "completed", date(2024, 1, 10), datetime(2024, 1, 10, 14, 30),             0.1),
    (103, 2,    Decimal("0.00"),   "cancelled", date(2024, 1, 12), datetime(2024, 1, 12, 8, 0),               None),   # zero amount; NULL double
    (104, 3,    Decimal("-15.75"), "refunded",  date(2024, 1, 20), datetime(2024, 1, 20, 17, 45),             0.0),    # negative amount
    (105, 3,    None,              "pending",   date(2024, 2, 1),  datetime(2024, 2, 1, 0, 0),                0.25),   # NULL amount
    (106, 4,    Decimal("999.99"), None,        date(2024, 2, 29), datetime(2024, 2, 29, 23, 59, 59, 999999), 0.0),    # leap day; microseconds; NULL status
    (107, None, Decimal("42.00"),  "completed", date(2024, 3, 2),  datetime(2024, 3, 2, 12, 0),               0.0),    # no customer
    (108, 99,   Decimal("10.00"),  "completed", date(2024, 3, 5),  datetime(2024, 3, 5, 10, 0),               0.0),    # unknown customer
    (109, 2,    Decimal("80.00"),  "completed", date(2024, 3, 8),  datetime(2024, 3, 8, 16, 20),              0.1),
    (109, 2,    Decimal("80.00"),  "completed", date(2024, 3, 8),  datetime(2024, 3, 8, 16, 20),              0.1),    # exact duplicate row
]
# fmt: on

TABLES = {
    "customers": (CUSTOMERS_SCHEMA, CUSTOMERS),
    "orders": (ORDERS_SCHEMA, ORDERS),
}
