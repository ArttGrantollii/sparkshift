"""Tests for the result comparator itself.

A comparator that always reports "equal" would make every equivalence test
pass and prove nothing, so it gets cases that must fail as well as pass.
These run on plain Python data and need no Spark.
"""

from decimal import Decimal

import pytest
from compare import (
    Snapshot,
    differences,
    limited_differences,
    ordered_differences,
    values_equal,
)

COLUMNS = (("id", "int"), ("value", "string"))


def snap(
    *rows: tuple[object, ...], columns: tuple[tuple[str, str], ...] = COLUMNS
) -> Snapshot:
    return Snapshot(columns, rows)


# --- Must pass ---------------------------------------------------------------


def test_identical_results_match() -> None:
    assert differences(snap((1, "a"), (2, "b")), snap((1, "a"), (2, "b"))) == []


def test_row_order_is_ignored() -> None:
    assert differences(snap((1, "a"), (2, "b")), snap((2, "b"), (1, "a"))) == []


def test_matching_duplicates_match() -> None:
    assert differences(snap((1, "a"), (1, "a")), snap((1, "a"), (1, "a"))) == []


def test_null_matches_null() -> None:
    assert differences(snap((1, None)), snap((1, None))) == []


def test_floats_match_within_tolerance() -> None:
    columns = (("x", "double"),)

    assert (
        differences(snap((0.1 + 0.2,), columns=columns), snap((0.3,), columns=columns))
        == []
    )


def test_nan_matches_nan() -> None:
    assert values_equal(float("nan"), float("nan"))


def test_nested_values_compare_element_by_element() -> None:
    assert values_equal(
        (1, [0.1 + 0.2, None], {"k": "v"}), (1, [0.3, None], {"k": "v"})
    )


# --- Must fail ---------------------------------------------------------------


def test_missing_duplicate_is_detected() -> None:
    problems = differences(snap((1, "a"), (1, "a")), snap((1, "a")))

    assert problems == ["Rows missing from the generated result (1):\n  (1, 'a')"]


def test_extra_row_is_detected() -> None:
    problems = differences(snap((1, "a")), snap((1, "a"), (2, "b")))

    assert problems == ["Unexpected rows in the generated result (1):\n  (2, 'b')"]


@pytest.mark.parametrize(
    ("expected", "actual"),
    [
        ((1, None), (1, "")),  # NULL is not an empty string
        ((None, "a"), (0, "a")),  # NULL is not zero
        ((1, "a"), (1, "A")),  # strings compare exactly
    ],
)
def test_different_values_are_detected(
    expected: tuple[object, ...], actual: tuple[object, ...]
) -> None:
    assert differences(snap(expected), snap(actual)) != []


def test_decimals_compare_exactly() -> None:
    columns = (("amount", "decimal(10,2)"),)

    assert (
        differences(
            snap((Decimal("1.00"),), columns=columns),
            snap((Decimal("1.01"),), columns=columns),
        )
        != []
    )


def test_floats_outside_tolerance_are_detected() -> None:
    assert not values_equal(1.0, 1.1)
    assert not values_equal(float("nan"), 0.0)


def test_booleans_do_not_match_integers() -> None:
    # In Python, True == 1. In a query result they are different values.
    assert not values_equal(True, 1)


@pytest.mark.parametrize(
    "actual_columns",
    [
        (("id", "bigint"), ("value", "string")),  # different type
        (("value", "string"), ("id", "int")),  # different order
        (("customer_id", "int"), ("value", "string")),  # different name
        (("id", "int"),),  # missing column
    ],
)
def test_column_differences_are_detected(
    actual_columns: tuple[tuple[str, str], ...],
) -> None:
    problems = differences(snap(), snap(columns=actual_columns))

    assert len(problems) == 1
    assert problems[0].startswith("Columns differ")


def test_long_differences_are_truncated() -> None:
    expected = snap(*[(i, "x") for i in range(15)])

    [problem] = differences(expected, snap())

    assert problem.startswith("Rows missing from the generated result (15):")
    assert problem.endswith("... and 5 more")


# --- LIMIT without ORDER BY: count + sub-multiset ----------------------------

UNLIMITED = snap((1, "a"), (1, "a"), (2, "b"), (3, None))


@pytest.mark.parametrize(
    "actual",
    [
        snap((3, None), (1, "a")),  # any rows, any order
        snap((1, "a"), (1, "a")),  # a duplicate, used as often as it occurs
        snap(),
    ],
)
def test_limited_result_from_the_unlimited_rows_matches(actual: Snapshot) -> None:
    assert limited_differences(UNLIMITED, actual, len(actual.rows)) == []


def test_limited_result_with_the_wrong_count_is_detected() -> None:
    problems = limited_differences(UNLIMITED, snap((1, "a")), expected_count=2)

    assert problems == ["Expected 2 rows, got 1"]


def test_limited_row_not_in_the_unlimited_result_is_detected() -> None:
    problems = limited_differences(UNLIMITED, snap((1, "a"), (9, "z")), 2)

    assert problems == ["Rows not in the unlimited result (1):\n  (9, 'z')"]


def test_limited_duplicate_used_too_often_is_detected() -> None:
    # (2, "b") occurs once in the unlimited result, so it can appear at most once.
    problems = limited_differences(UNLIMITED, snap((2, "b"), (2, "b")), 2)

    assert problems == ["Rows not in the unlimited result (1):\n  (2, 'b')"]


def test_limited_result_with_different_columns_is_detected() -> None:
    actual = snap((1,), columns=(("id", "int"),))

    [problem] = limited_differences(UNLIMITED, actual, 1)

    assert problem.startswith("Columns differ")


# --- ORDER BY: tie groups in sequence ----------------------------------------

# Sorted by id, NULLs last. id 1 has three tied rows; id 3 has two.
SORTED = snap((1, "a"), (1, "b"), (1, "a"), (2, "c"), (3, "d"), (3, "e"), (None, "f"))


@pytest.mark.parametrize(
    "actual",
    [
        SORTED,
        # Tied rows in another order.
        snap((1, "b"), (1, "a"), (1, "a"), (2, "c"), (3, "e"), (3, "d"), (None, "f")),
    ],
)
def test_ordered_result_with_ties_in_any_order_matches(actual: Snapshot) -> None:
    assert ordered_differences(SORTED, actual, ["id"]) == []


@pytest.mark.parametrize(
    ("actual", "first_problem"),
    [
        (
            # Groups swapped: ascending vs descending.
            snap(
                (None, "f"), (3, "d"), (3, "e"), (2, "c"), (1, "a"), (1, "b"), (1, "a")
            ),
            "Rows 1 to 3 are out of order",
        ),
        (
            # NULLs first instead of last.
            snap(
                (None, "f"), (1, "a"), (1, "b"), (1, "a"), (2, "c"), (3, "d"), (3, "e")
            ),
            "Rows 1 to 3 are out of order",
        ),
        (
            # A row moved across a group boundary.
            snap(
                (1, "a"), (1, "b"), (2, "c"), (1, "a"), (3, "d"), (3, "e"), (None, "f")
            ),
            "Rows 1 to 3 are out of order",
        ),
        (
            # Same rows within a group, but a duplicate changed.
            snap(
                (1, "a"), (1, "b"), (1, "b"), (2, "c"), (3, "d"), (3, "e"), (None, "f")
            ),
            "Rows 1 to 3 are out of order",
        ),
        (
            snap((1, "a"), (1, "b"), (1, "a"), (2, "c"), (3, "d"), (3, "e")),
            "Expected 7 rows, got 6",
        ),
    ],
)
def test_ordered_result_out_of_order_is_detected(
    actual: Snapshot, first_problem: str
) -> None:
    problems = ordered_differences(SORTED, actual, ["id"])

    assert problems
    assert problems[0].startswith(first_problem)


def test_ordered_difference_shows_expected_and_found_rows() -> None:
    actual = snap(
        (1, "a"), (1, "b"), (2, "c"), (1, "a"), (3, "d"), (3, "e"), (None, "f")
    )

    assert ordered_differences(SORTED, actual, ["id"]) == [
        "Rows 1 to 3 are out of order (rows that tie on id may come in any order)",
        "Expected at these positions (1):\n  (1, 'a')",
        "Found instead (1):\n  (2, 'c')",
    ]


def test_ordering_on_all_columns_requires_the_exact_sequence() -> None:
    swapped = snap(
        (1, "b"), (1, "a"), (1, "a"), (2, "c"), (3, "d"), (3, "e"), (None, "f")
    )

    assert ordered_differences(SORTED, swapped, ["id", "value"]) != []


@pytest.mark.parametrize(
    "actual",
    [
        snap((1, "a"), (1, "b"), (1, "a"), (2, "c"), (3, "d")),
        snap((1, "a"), (1, "b"), (1, "a"), (2, "c"), (3, "e")),  # either tied row
        snap((1, "a"), (1, "a")),  # cut inside the first group
        snap(),
    ],
)
def test_limit_may_cut_the_last_group_of_ties(actual: Snapshot) -> None:
    assert ordered_differences(SORTED, actual, ["id"], len(actual.rows)) == []


@pytest.mark.parametrize(
    "actual",
    [
        snap((1, "a"), (1, "b"), (1, "a"), (2, "c"), (None, "f")),  # skips id 3
        snap((1, "a"), (1, "b"), (1, "a"), (2, "c"), (3, "z")),  # not in the group
        snap((1, "b"), (1, "b")),  # (1, "b") occurs only once
    ],
)
def test_limit_with_rows_from_outside_the_cut_group_is_detected(
    actual: Snapshot,
) -> None:
    assert ordered_differences(SORTED, actual, ["id"], len(actual.rows)) != []


def test_limited_ordered_result_with_the_wrong_count_is_detected() -> None:
    actual = snap((1, "a"), (1, "b"))

    assert ordered_differences(SORTED, actual, ["id"], 3) == ["Expected 3 rows, got 2"]


def test_ordered_result_with_different_columns_is_detected() -> None:
    actual = snap((1,), columns=(("id", "int"),))

    [problem] = ordered_differences(SORTED, actual, ["id"])

    assert problem.startswith("Columns differ")


def test_order_keys_must_be_output_columns() -> None:
    with pytest.raises(ValueError, match="order_keys must be output columns"):
        ordered_differences(SORTED, SORTED, ["amount"])
