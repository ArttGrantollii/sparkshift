"""Tests for running the test tables and queries on other databases, without
a database."""

import datetime
from decimal import Decimal

import pytest
from compare import Snapshot, differences
from databases import (
    MYSQL,
    POSTGRES,
    REQUIRED_VARIABLE,
    Engine,
    cases,
    comparable,
    create_table_sql,
    insert_sql,
    skip_unless_configured,
)
from datasets import ORDERS_SCHEMA, TABLES
from pyspark.sql.types import (
    ArrayType,
    DecimalType,
    IntegerType,
    TimestampNTZType,
    TimestampType,
)
from test_formatting import MODULES


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        (POSTGRES, ["numeric(10,2)", "integer", "timestamptz", "timestamp"]),
        (MYSQL, ["decimal(10,2)", "int", "timestamp(6)", "datetime(6)"]),
    ],
)
def test_column_types(engine: Engine, expected: list[str]) -> None:
    spark_types = [
        DecimalType(10, 2),
        IntegerType(),
        TimestampType(),
        TimestampNTZType(),
    ]

    assert [engine.column_type(t) for t in spark_types] == expected


@pytest.mark.parametrize("engine", [POSTGRES, MYSQL])
def test_unknown_types_are_rejected(engine: Engine) -> None:
    with pytest.raises(ValueError, match=f"No {engine.name} type for Spark type array"):
        engine.column_type(ArrayType(IntegerType()))


def test_create_and_insert_statements() -> None:
    assert create_table_sql(POSTGRES, "orders", ORDERS_SCHEMA) == (
        "CREATE TABLE orders (order_id integer, customer_id integer, "
        "amount numeric(10,2), status text, order_date date, "
        "created_at timestamptz, discount double precision)"
    )
    assert create_table_sql(MYSQL, "orders", ORDERS_SCHEMA) == (
        "CREATE TABLE orders (order_id int, customer_id int, "
        "amount decimal(10,2), status text, order_date date, "
        "created_at timestamp(6), discount double)"
    )
    assert insert_sql("orders", ORDERS_SCHEMA) == (
        "INSERT INTO orders VALUES (%s, %s, %s, %s, %s, %s, %s)"
    )


@pytest.mark.parametrize("engine", [POSTGRES, MYSQL])
def test_every_test_table_has_types_on_every_engine(engine: Engine) -> None:
    for name, (schema, _) in TABLES.items():
        create_table_sql(engine, name, schema)


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


def test_numbers_share_a_type_per_column() -> None:
    expected, actual = comparable(
        _snapshot(["avg", "id", "flag"], (Decimal("2.5"), 1, True)),
        _snapshot(["avg", "id", "flag"], (2.5, Decimal("1"), True)),
    )

    assert expected.rows == ((2.5, Decimal("1"), True),)
    assert isinstance(expected.rows[0][1], Decimal)
    assert differences(expected, actual) == []


def test_exact_numbers_stay_exact_without_floats() -> None:
    expected, actual = comparable(
        _snapshot(["amount"], (Decimal("1.50"),)),
        _snapshot(["amount"], (Decimal("1.5"),)),
    )

    assert expected.rows == ((Decimal("1.50"),),)
    assert differences(expected, actual) == []


def test_zero_and_one_are_booleans_next_to_booleans() -> None:
    # MySQL returns comparisons as integers.
    expected, actual = comparable(
        _snapshot(["big", "count"], (1, 1), (0, 0), (None, 2)),
        _snapshot(["big", "count"], (True, 1), (False, 0), (None, 2)),
    )

    assert expected.rows == ((True, 1), (False, 0), (None, 2))
    assert differences(expected, actual) == []


def test_other_integers_are_not_booleans() -> None:
    expected, actual = comparable(_snapshot(["b"], (2,)), _snapshot(["b"], (True,)))

    assert differences(expected, actual) != []


def test_a_date_never_matches_a_timestamp() -> None:
    expected, actual = comparable(
        _snapshot(["d"], (datetime.datetime(2024, 1, 6),)),
        _snapshot(["d"], (datetime.date(2024, 1, 6),)),
    )

    assert differences(expected, actual) != []


@pytest.mark.parametrize(
    ("url", "required", "skipped"),
    [
        (None, None, True),
        ("mysql://root@127.0.0.1:3306", None, False),
        # Required in this job: run, and fail without a URL.
        (None, "mysql", False),
        # Another database's job.
        (None, "postgres", True),
    ],
)
def test_database_tests_skip_unless_configured_or_required(
    monkeypatch: pytest.MonkeyPatch,
    url: str | None,
    required: str | None,
    skipped: bool,
) -> None:
    for variable, value in [(MYSQL.url_variable, url), (REQUIRED_VARIABLE, required)]:
        if value is None:
            monkeypatch.delenv(variable, raising=False)
        else:
            monkeypatch.setenv(variable, value)

    assert skip_unless_configured(MYSQL).args[0] is skipped


@pytest.mark.parametrize(
    ("dialect", "examples"),
    [("postgres", ["postgres/latest_order"]), ("mysql", ["mysql/name_lengths"])],
)
def test_every_scenario_and_example_of_a_dialect_runs_on_its_database(
    dialect: str, examples: list[str]
) -> None:
    scenarios = [
        name
        for module in MODULES
        for name, (scenario_dialect, *_) in getattr(
            module, "DIALECT_SCENARIOS", {}
        ).items()
        if scenario_dialect == dialect
    ]

    assert [case.id for case in cases(dialect)] == [*scenarios, *examples]
