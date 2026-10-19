"""Tests for reading SAS data lines with list input."""

import datetime

import pytest

from sparkshift.sas_datalines import Field, informat_kind, read

NUMBER = Field("x", "number")
TEXT = Field("name", "text", 8)


def rows(text: str, *fields: Field) -> list[tuple[object, ...]]:
    found, notes, issues = read(text, fields, 4)
    assert notes == [] and issues == []
    return found


def messages(text: str, *fields: Field) -> tuple[list[str], list[str]]:
    _, notes, issues = read(text, fields, 4)
    return [note.message for note in notes], [issue.message for issue in issues]


@pytest.mark.parametrize(
    ("informat", "kind"),
    [
        ("$20.", ("text", 20)),
        ("$1.", ("text", 1)),
        ("8.", ("number", None)),
        ("8.2", ("number", None)),
        ("BEST12.", ("number", None)),
        ("BEST.", ("number", None)),
        ("DATE9.", ("date", None)),
        ("DATE.", ("date", None)),
        ("YYMMDD10.", ("date", None)),
        ("MMDDYY10.", ("date", None)),
        ("DDMMYY8.", ("date", None)),
        (".", None),
        ("$.", None),
        ("$CHAR20.", None),
        ("COMMA10.2", None),
        ("DATETIME20.", None),
    ],
)
def test_the_informats_read(informat: str, kind: object) -> None:
    assert informat_kind(informat) == kind


def test_values_are_separated_by_blanks() -> None:
    text = "101 John\n102   Sarah  \n"

    assert rows(text, NUMBER, TEXT) == [(101.0, "John"), (102.0, "Sarah")]


def test_a_period_is_a_missing_value_for_numbers_and_text() -> None:
    assert rows(". .\n", NUMBER, TEXT) == [(None, None)]


@pytest.mark.parametrize(
    ("value", "number"),
    [
        ("3", 3.0),
        ("-2.5", -2.5),
        ("+7", 7.0),
        (".5", 0.5),
        ("5.", 5.0),
        ("1e3", 1000.0),
        ("1.5E-2", 0.015),
        (".A", None),
        ("._", None),
    ],
)
def test_numbers(value: str, number: float | None) -> None:
    assert rows(value, NUMBER) == [(number,)]


@pytest.mark.parametrize("value", ["abc", "1,000", "$5", "1-2", "e3", "--1"])
def test_invalid_numbers_are_missing_with_a_note(value: str) -> None:
    found, notes, issues = read(f"{value}\n", [NUMBER], 4)

    assert found == [(None,)]
    assert [(n.message, n.line, n.text) for n in notes] == [
        ("invalid data for x", 4, value)
    ]
    assert issues == []


def test_decimals_of_a_w_d_informat_apply_without_a_decimal_point() -> None:
    field = Field("x", "number", informat="8.2")

    assert rows(
        "1234 12.5 1e2",
        field,
        Field("y", "number", informat="8.2"),
        Field("z", "number", informat="8.2"),
    ) == [(12.34, 12.5, 100.0)]


def test_text_longer_than_the_variable_is_truncated_with_a_note() -> None:
    found, notes, _ = read("Christopher\nAnn\nBartholomew\n", [TEXT], 4)

    assert found == [("Christop",), ("Ann",), ("Bartholo",)]
    assert len(notes) == 1
    assert notes[0].message == "text truncated to the length of name"
    assert (notes[0].line, notes[0].text) == (4, "Christopher")
    assert "'Christopher' as 'Christop'" in notes[0].hint
    assert notes[0].hint.endswith("Also on line 6.")


def test_a_text_variable_keeps_its_length() -> None:
    assert rows("Christopher", Field("name", "text", 20)) == [("Christopher",)]


@pytest.mark.parametrize(
    ("informat", "value", "date"),
    [
        ("DATE9.", "01JAN2024", datetime.date(2024, 1, 1)),
        ("DATE9.", "5feb2024", datetime.date(2024, 2, 5)),
        ("DATE11.", "05-Feb-2024", datetime.date(2024, 2, 5)),
        ("YYMMDD10.", "2024-03-05", datetime.date(2024, 3, 5)),
        ("YYMMDD8.", "20240305", datetime.date(2024, 3, 5)),
        ("MMDDYY10.", "03/05/2024", datetime.date(2024, 3, 5)),
        ("DDMMYY10.", "05/03/2024", datetime.date(2024, 3, 5)),
        ("DDMMYY10.", "05.03.2024", datetime.date(2024, 3, 5)),
    ],
)
def test_dates(informat: str, value: str, date: datetime.date) -> None:
    assert rows(value, Field("d", "date", informat=informat)) == [(date,)]


@pytest.mark.parametrize(
    ("informat", "value"),
    [
        ("DATE9.", "31FEB2024"),
        ("DATE9.", "01XYZ2024"),
        ("DATE9.", "2024-01-01"),
        ("YYMMDD10.", "2024-13-01"),
        ("MMDDYY10.", "2024-03-05"),
        ("YYMMDD10.", "2024-03/05"),
    ],
)
def test_invalid_dates_are_missing_with_a_note(informat: str, value: str) -> None:
    found, notes, issues = read(value, [Field("d", "date", informat=informat)], 4)

    assert found == [(None,)]
    assert [note.message for note in notes] == ["invalid data for d"]
    assert issues == []


@pytest.mark.parametrize(
    ("informat", "value"),
    [("DATE7.", "01JAN24"), ("MMDDYY8.", "03/05/24"), ("DDMMYY6.", "050324")],
)
def test_two_digit_years_are_an_issue(informat: str, value: str) -> None:
    found, notes, issues = read(value, [Field("d", "date", informat=informat)], 4)

    assert found == [(None,)]
    assert notes == []
    assert [issue.message for issue in issues] == [f"two-digit year in {informat}"]


def test_a_short_line_continues_on_the_next_flowover() -> None:
    found, notes, _ = read("1\nAnn\n2 Bob\n3\nCy\n", [NUMBER, TEXT], 4)

    assert found == [(1.0, "Ann"), (2.0, "Bob"), (3.0, "Cy")]
    assert [(n.message, n.line) for n in notes] == [
        ("data lines with fewer values than INPUT reads", 4)
    ]
    assert notes[0].hint.startswith("On lines 4, 7, SAS goes on to the next line")


def test_a_blank_line_is_skipped_as_a_short_line() -> None:
    found, notes, _ = read("1 Ann\n\n2 Bob\n", [NUMBER, TEXT], 4)

    assert found == [(1.0, "Ann"), (2.0, "Bob")]
    assert [n.message for n in notes] == [
        "data lines with fewer values than INPUT reads"
    ]


def test_an_incomplete_last_observation_is_not_written_lost_card() -> None:
    found, notes, _ = read("1 Ann\n2\n", [NUMBER, TEXT], 4)

    assert found == [(1.0, "Ann")]
    assert [(n.message, n.line) for n in notes] == [
        ("the last observation of the data lines is incomplete", 5),
        ("data lines with fewer values than INPUT reads", 5),
    ]


def test_values_beyond_those_input_reads_are_ignored() -> None:
    assert rows("1 Ann extra values\n", NUMBER, TEXT) == [(1.0, "Ann")]


def test_blank_lines_at_the_end_write_nothing() -> None:
    assert rows("1 Ann\n\n   \n", NUMBER, TEXT) == [(1.0, "Ann")]


def test_no_data_lines_no_rows() -> None:
    assert rows("", NUMBER) == []


def test_a_tab_is_an_issue() -> None:
    found, notes, issues = read("1 Ann\n2\tBob\n", [NUMBER, TEXT], 4)

    assert found == [] and notes == []
    assert [(i.message, i.line) for i in issues] == [("tab in data lines", 5)]


def test_notes_name_up_to_five_other_lines() -> None:
    text = "".join(f"x{i}\n" for i in range(9))
    _, notes, _ = read(text, [NUMBER], 4)

    assert notes[0].hint.endswith("Also on lines 5, 6, 7, 8, 9, and 3 more.")
