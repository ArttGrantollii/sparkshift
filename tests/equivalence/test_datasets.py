"""Guard the edge cases in the test data.

The equivalence tests are only as strong as their data. These checks fail if
someone "cleans up" a dataset and removes a row a test relies on.
"""

from collections import Counter

from datasets import CUSTOMERS, ORDERS, TABLES


def test_every_row_matches_its_schema_width() -> None:
    for name, (schema, rows) in TABLES.items():
        for row in rows:
            assert len(row) == len(schema.fields), f"{name}: {row}"


def test_tables_contain_exact_duplicate_rows() -> None:
    for rows in (CUSTOMERS, ORDERS):
        assert any(count > 1 for count in Counter(rows).values())


def test_customers_contain_null_and_empty_strings() -> None:
    names = [row[1] for row in CUSTOMERS]

    assert None in names
    assert "" in names


def test_every_customers_column_contains_a_null() -> None:
    for column in range(1, len(CUSTOMERS[0])):
        assert any(row[column] is None for row in CUSTOMERS), f"column {column}"


def test_orders_cover_unmatched_and_missing_customers() -> None:
    customer_ids = {row[0] for row in CUSTOMERS}
    order_customer_ids = {row[1] for row in ORDERS}

    assert None in order_customer_ids  # order without a customer
    assert order_customer_ids - customer_ids - {None}  # order for an unknown customer
    assert customer_ids - order_customer_ids  # customer without orders


def test_order_amounts_include_zero_negative_and_null() -> None:
    amounts = [row[2] for row in ORDERS]

    assert None in amounts
    assert any(amount is not None and amount == 0 for amount in amounts)
    assert any(amount is not None and amount < 0 for amount in amounts)
