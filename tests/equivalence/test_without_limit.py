"""Tests for building the unlimited reference query. No Spark needed."""

import pytest
from harness import without_limit


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t LIMIT 3", "SELECT * FROM t"),
        (
            "SELECT DISTINCT a FROM t WHERE b > 1 LIMIT 2",
            "SELECT DISTINCT a FROM t WHERE b > 1",
        ),
    ],
)
def test_limit_without_order_by_is_removed(sql: str, expected: str) -> None:
    assert without_limit(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t",  # nothing to remove
        "SELECT * FROM t ORDER BY a LIMIT 3",  # ordered: compared exactly instead
    ],
)
def test_queries_compared_exactly_are_left_alone(sql: str) -> None:
    assert without_limit(sql) is None
