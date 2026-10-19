"""Read SAS data lines with list input, as a DATA step with INPUT and
DATALINES does, at conversion time: the values become rows in the code.

The rules are those of list input in *SAS 9.4 Statements: Reference*, "INPUT
Statement, List" and the informats' pages (see docs/sas.md):

- Values are separated by blanks. A period alone is a missing value, for
  numbers and for text.
- A text value is stored at the variable's length: the LENGTH statement's,
  else the width of a $w. informat, else 8; longer values are truncated.
- A number is read by the standard numeric informat (with w.d and no decimal
  point in the data, divided by 10 to the power d); anything else is invalid
  data, stored as a missing value.
- Dates use the DATEw., YYMMDDw., MMDDYYw., and DDMMYYw. informats. Two-digit
  years are refused: their century depends on the YEARCUTOFF= option.
- When a line has fewer values than INPUT reads, SAS goes on to the next line
  (FLOWOVER); when the data end before INPUT has read every value, that last
  observation is not written (LOST CARD).

The SAS front end checks what this module does not: that every informat is
one listed here.
"""

import datetime
import re
from collections.abc import Sequence
from dataclasses import dataclass

# The informats this module reads, by family: a $w. width, numbers, dates.
_TEXT_INFORMAT = re.compile(r"\$(\d+)\.")
_NUMBER_INFORMAT = re.compile(r"(?:BEST)?(\d+)?\.(\d+)?")
_DATE_INFORMATS = re.compile(r"(DATE|YYMMDD|MMDDYY|DDMMYY)(\d+)?\.")
_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
# The special missing values .A to .Z and ._, read as missing.
_SPECIAL_MISSING = re.compile(r"\.[A-Za-z_]")
_MONTHS = {
    name: number
    for number, name in enumerate(
        (
            "JAN",
            "FEB",
            "MAR",
            "APR",
            "MAY",
            "JUN",
            "JUL",
            "AUG",
            "SEP",
            "OCT",
            "NOV",
            "DEC",
        ),
        1,
    )
}
_DATE_PATTERNS = {
    "DATE": re.compile(r"(\d{1,2})[-/.]?([A-Za-z]{3})[-/.]?(\d{2}|\d{4})"),
    "YYMMDD": re.compile(r"(\d{4})([-/.]?)(\d{1,2})\2(\d{1,2})"),
    "MMDDYY": re.compile(r"(\d{1,2})([-/.]?)(\d{1,2})\2(\d{2}|\d{4})"),
    "DDMMYY": re.compile(r"(\d{1,2})([-/.]?)(\d{1,2})\2(\d{2}|\d{4})"),
}

DEFAULT_TEXT_LENGTH = 8


@dataclass(frozen=True)
class Field:
    """A variable INPUT reads: ``kind`` is "text", "number", or "date";
    ``length`` a text variable's length; ``informat`` as written, upper
    case."""

    name: str
    kind: str
    length: int | None = None
    informat: str | None = None


@dataclass(frozen=True)
class Note:
    """Something SAS would note in its log while reading, on ``line``."""

    message: str
    line: int
    text: str
    hint: str


def informat_kind(informat: str) -> tuple[str, int | None] | None:
    """The kind of value an informat reads, and a text informat's width; None
    for an informat this module does not read."""
    text = _TEXT_INFORMAT.fullmatch(informat)
    if text:
        return "text", int(text.group(1))
    if _DATE_INFORMATS.fullmatch(informat):
        return "date", None
    if informat != "." and _NUMBER_INFORMAT.fullmatch(informat):
        return "number", None
    return None


def read(
    text: str, fields: Sequence[Field], first_line: int
) -> tuple[list[tuple[object, ...]], list[Note], list[Note]]:
    """The observations list input reads from the data lines ``text`` (whose
    first line is ``first_line`` of the program): one tuple of values per
    observation, in the order of ``fields``. Also the notes SAS would write,
    and the values that cannot be read safely (issues)."""
    lines_of_text = text.splitlines()
    rows: list[tuple[object, ...]] = []
    notes: list[Note] = []
    issues: list[Note] = []
    tabs = [number for number, line in enumerate(lines_of_text) if "\t" in line]
    if tabs:
        issues.append(
            Note(
                "tab in data lines",
                first_line + tabs[0],
                lines_of_text[tabs[0]],
                "List input separates values with blanks; SAS reads a tab as part "
                "of a value unless INFILE has EXPANDTABS. Separate values with "
                "blanks.",
            )
        )
        return rows, notes, issues
    records = [line.split(" ") for line in lines_of_text]
    records = [[token for token in record if token] for record in records]
    # The observations that start on a line without enough values.
    flowed: list[int] = []
    position = 0
    while position < len(records):
        start = position
        tokens, index = records[position], 0
        values: list[object] = []
        complete = True
        for field in fields:
            while index >= len(tokens):
                if start not in flowed:
                    flowed.append(start)
                position += 1
                if position >= len(records):
                    complete = False
                    break
                tokens, index = records[position], 0
            if not complete:
                break
            line = first_line + position
            values.append(_value(field, tokens[index], line, notes, issues))
            index += 1
        if not complete:
            if not values:
                # Only blank lines were left: the data have ended.
                flowed.remove(start)
            else:
                notes.append(
                    Note(
                        "the last observation of the data lines is incomplete",
                        first_line + start,
                        lines_of_text[start],
                        "SAS reaches the end of the data before INPUT has read every "
                        "value (LOST CARD) and does not write that observation.",
                    )
                )
            break
        rows.append(tuple(values))
        position += 1
    lines = [first_line + start for start in flowed]
    if lines:
        notes.append(
            Note(
                "data lines with fewer values than INPUT reads",
                lines[0],
                lines_of_text[lines[0] - first_line],
                f"On {_lines(lines)}, SAS goes on to the next line for the rest "
                "(FLOWOVER), so one observation reads several lines. Check the "
                "data.",
            )
        )
    return rows, _grouped(notes), _grouped(issues)


def _value(
    field: Field, token: str, line: int, notes: list[Note], issues: list[Note]
) -> object:
    if token == ".":
        return None
    if field.kind == "text":
        length = field.length or DEFAULT_TEXT_LENGTH
        if len(token) > length:
            notes.append(
                Note(
                    f"text truncated to the length of {field.name}",
                    line,
                    token,
                    f"{field.name} is {length} characters long, so SAS stores "
                    f"{token!r} as {token[:length]!r}. Give it a LENGTH statement "
                    "before INPUT to keep longer values.",
                )
            )
        return token[:length]
    if field.kind == "number":
        return _number(field, token, line, notes)
    return _date(field, token, line, notes, issues)


def _number(field: Field, token: str, line: int, notes: list[Note]) -> float | None:
    if _SPECIAL_MISSING.fullmatch(token):
        return None
    if not _NUMBER.fullmatch(token):
        notes.append(_invalid(field, token, line))
        return None
    value = float(token)
    decimals = _NUMBER_INFORMAT.fullmatch(field.informat) if field.informat else None
    places = decimals.group(2) if decimals else None
    if places and "." not in token and "e" not in token.lower():
        # w.d with no decimal point in the data: the last d digits are the
        # decimals.
        value /= 10 ** int(places)
    return value


def _date(
    field: Field, token: str, line: int, notes: list[Note], issues: list[Note]
) -> datetime.date | None:
    assert field.informat is not None
    family_match = _DATE_INFORMATS.fullmatch(field.informat)
    assert family_match is not None
    family = family_match.group(1)
    match = _DATE_PATTERNS[family].fullmatch(token)
    if match is None:
        notes.append(_invalid(field, token, line))
        return None
    if family == "DATE":
        day, month_name, year = match.group(1), match.group(2), match.group(3)
        month = _MONTHS.get(month_name.upper())
        if month is None:
            notes.append(_invalid(field, token, line))
            return None
        parts = (year, month, day)
    elif family == "YYMMDD":
        parts = (match.group(1), match.group(3), match.group(4))
    elif family == "MMDDYY":
        parts = (match.group(4), match.group(1), match.group(3))
    else:
        parts = (match.group(4), match.group(3), match.group(1))
    year_text = str(parts[0])
    if len(year_text) == 2:
        issues.append(
            Note(
                f"two-digit year in {field.informat}",
                line,
                token,
                "SAS chooses its century with the YEARCUTOFF= option, which "
                "SparkShift cannot see. Write four-digit years.",
            )
        )
        return None
    try:
        return datetime.date(int(year_text), int(parts[1]), int(parts[2]))
    except ValueError:
        notes.append(_invalid(field, token, line))
        return None


def _invalid(field: Field, token: str, line: int) -> Note:
    what = "a date" if field.kind == "date" else "a number"
    return Note(
        f"invalid data for {field.name}",
        line,
        token,
        f"{token!r} is not {what} SAS can read, so SAS stores a missing value and "
        "writes a note.",
    )


def _grouped(notes: list[Note]) -> list[Note]:
    """One note per message, the first, with the lines of the others."""
    first: dict[str, Note] = {}
    lines: dict[str, list[int]] = {}
    for note in notes:
        first.setdefault(note.message, note)
        lines.setdefault(note.message, []).append(note.line)
    grouped = []
    for message, note in first.items():
        others = [line for line in lines[message] if line != note.line]
        hint = note.hint
        if others:
            hint += f" Also on {_lines(others)}."
        grouped.append(Note(message, note.line, note.text, hint))
    return grouped


def _lines(lines: list[int]) -> str:
    """ "line 4", "lines 4, 7", or "lines 4, 5, 6, 7, 8, and 3 more"."""
    unique = sorted(set(lines))
    if len(unique) == 1:
        return f"line {unique[0]}"
    shown = ", ".join(str(line) for line in unique[:5])
    more = f", and {len(unique) - 5} more" if len(unique) > 5 else ""
    return f"lines {shown}{more}"
