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


def test_product_names_cover_whitespace_and_text_edge_cases() -> None:
    from datasets import PRODUCTS

    names = [row[1] for row in PRODUCTS]

    assert any(n and n != n.lstrip() and n != n.rstrip() for n in names)  # both sides
    assert any(
        n and n == n.lstrip() and n != n.rstrip() for n in names
    )  # trailing only
    assert any(n and len(n.encode()) > len(n) for n in names)  # multi-byte characters
    assert "" in names
    assert None in names


def test_product_numbers_cover_rounding_boundaries() -> None:
    from decimal import Decimal

    from datasets import PRODUCTS

    prices = [row[3] for row in PRODUCTS]
    weights = [row[4] for row in PRODUCTS]

    assert Decimal("2.500") in prices
    assert Decimal("-2.500") in prices
    assert 2.5 in weights
    assert -2.5 in weights
    assert None in prices
    assert any(
        stock is not None and stock < 0 for stock in (row[5] for row in PRODUCTS)
    )
