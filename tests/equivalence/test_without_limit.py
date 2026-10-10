"""Tests for building the unlimited reference query, and for choosing how to
compare results. No Spark needed."""

import pytest
from compare import Snapshot
from harness import has_order_by, result_differences, without_limit


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t LIMIT 3", "SELECT * FROM t"),
        (
            "SELECT DISTINCT a FROM t WHERE b > 1 LIMIT 2",
            "SELECT DISTINCT a FROM t WHERE b > 1",
        ),
        # Ordered: the unlimited result tells which rows tie with the last one.
        ("SELECT * FROM t ORDER BY a DESC LIMIT 3", "SELECT * FROM t ORDER BY a DESC"),
    ],
)
def test_limit_is_removed(sql: str, expected: str) -> None:
    assert without_limit(sql) == expected


def test_query_without_limit_is_left_alone() -> None:
    assert without_limit("SELECT * FROM t ORDER BY a") is None


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t ORDER BY a", True),
        ("SELECT * FROM t LIMIT 3", False),
        # An ORDER BY inside a window function does not order the result.
        ("SELECT ROW_NUMBER() OVER (ORDER BY a) AS n FROM t", False),
    ],
)
def test_has_order_by(sql: str, expected: bool) -> None:
    assert has_order_by(sql) is expected


def test_set_operations_have_their_own_order_by_and_limit() -> None:
    sql = "SELECT a FROM t UNION SELECT a FROM u ORDER BY a LIMIT 3"

    assert has_order_by(sql) is True
    assert without_limit(sql) == "SELECT a FROM t UNION SELECT a FROM u ORDER BY a"


def test_other_dialects_are_read_and_written_in_their_own_syntax() -> None:
    sql = "SELECT a FROM t ORDER BY a NULLS FIRST LIMIT 3"

    assert has_order_by(sql, "postgres") is True
    assert without_limit(sql, "postgres") == "SELECT a FROM t ORDER BY a NULLS FIRST"


ONE_TWO = Snapshot((("n", "int"),), ((1,), (2,)))
TWO_ONE = Snapshot((("n", "int"),), ((2,), (1,)))


def test_order_keys_make_the_order_count() -> None:
    assert result_differences(ONE_TWO, TWO_ONE, None, None) == []
    assert result_differences(ONE_TWO, TWO_ONE, ["n"], None) != []


def test_a_limit_without_order_keys_accepts_any_rows_of_the_result() -> None:
    first = Snapshot(ONE_TWO.columns, ((1,),))
    other = Snapshot(ONE_TWO.columns, ((3,),))

    assert result_differences(ONE_TWO, first, None, 1) == []
    assert result_differences(ONE_TWO, other, None, 1) != []
    assert result_differences(ONE_TWO, ONE_TWO, None, 1) != []
