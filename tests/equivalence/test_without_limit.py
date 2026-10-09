"""Tests for building the unlimited reference query. No Spark needed."""

import pytest
from harness import has_order_by, without_limit


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
