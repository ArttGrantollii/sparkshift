"""Tests for running the test tables and queries on other databases, without
a database."""

import datetime
from decimal import Decimal

import pytest
from compare import Snapshot, differences
from databases import comparable, create_table_sql, insert_sql, postgres_type
from datasets import ORDERS_SCHEMA, TABLES
from pyspark.sql.types import (
    ArrayType,
    DecimalType,
    IntegerType,
    TimestampNTZType,
    TimestampType,
)
from test_formatting import MODULES
from test_postgres import postgres_cases


def test_postgres_types() -> None:
    assert postgres_type(DecimalType(10, 2)) == "numeric(10,2)"
    assert postgres_type(IntegerType()) == "integer"
    assert postgres_type(TimestampType()) == "timestamptz"
    assert postgres_type(TimestampNTZType()) == "timestamp"


def test_unknown_types_are_rejected() -> None:
    with pytest.raises(ValueError, match="No PostgreSQL type for Spark type array"):
        postgres_type(ArrayType(IntegerType()))


def test_create_and_insert_statements() -> None:
    assert create_table_sql("orders", ORDERS_SCHEMA) == (
        "CREATE TABLE orders (order_id integer, customer_id integer, "
        "amount numeric(10,2), status text, order_date date, "
        "created_at timestamptz, discount double precision)"
    )
    assert insert_sql("orders", ORDERS_SCHEMA) == (
        "INSERT INTO orders VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )


def test_every_test_table_has_postgres_types() -> None:
    for name, (schema, _) in TABLES.items():
        create_table_sql(name, schema)


def _snapshot(names: list[str], *rows: tuple[object, ...]) -> Snapshot:
    return Snapshot(tuple((name, "int") for name in names), tuple(rows))


def test_names_are_compared_ignoring_case_and_types_are_not() -> None:
    expected, actual = comparable(
        _snapshot(["total"], (1,)), Snapshot((("Total", "bigint"),), ((1,),))
    )

    assert expected == actual


def test_timestamps_with_a_time_zone_become_utc_wall_clock() -> None:
    plus_two = datetime.timezone(datetime.timedelta(hours=2))
    aware = datetime.datetime(2024, 3, 1, 2, 30, tzinfo=plus_two)

    expected, actual = comparable(
        _snapshot(["t"], (aware,)),
        _snapshot(["t"], (datetime.datetime(2024, 3, 1, 0, 30),)),
    )

    assert expected == actual


def test_exact_numbers_compare_as_floats_next_to_floats() -> None:
    expected, actual = comparable(
        _snapshot(["avg", "id", "flag"], (Decimal("2.5"), 1, True)),
        _snapshot(["avg", "id", "flag"], (2.5, Decimal("1"), True)),
    )

    assert expected.rows == ((2.5, 1, True),)
    assert differences(expected, actual) == []


def test_exact_numbers_stay_exact_without_floats() -> None:
    expected, actual = comparable(
        _snapshot(["amount"], (Decimal("1.50"),)),
        _snapshot(["amount"], (Decimal("1.5"),)),
    )

    assert expected.rows == ((Decimal("1.50"),),)
    assert differences(expected, actual) == []


def test_a_date_never_matches_a_timestamp() -> None:
    expected, actual = comparable(
        _snapshot(["d"], (datetime.datetime(2024, 1, 6),)),
        _snapshot(["d"], (datetime.date(2024, 1, 6),)),
    )

    assert differences(expected, actual) != []


def test_every_postgres_scenario_and_example_runs_on_postgres() -> None:
    scenarios = [
        name
        for module in MODULES
        for name, (dialect, *_) in getattr(module, "DIALECT_SCENARIOS", {}).items()
        if dialect == "postgres"
    ]

    ids = [case.id for case in postgres_cases()]

    assert ids == [*scenarios, "postgres/latest_order"]
