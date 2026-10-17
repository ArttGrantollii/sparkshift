"""The rules for converting many files, shared by the command line and the
playground (their own tests check each uses them)."""

from pathlib import PurePosixPath

import pytest

from sparkshift.batch import colliding, dialect_of, source_suffixes


def test_sql_and_sas_files_are_converted_or_only_sas_files() -> None:
    assert source_suffixes(None) == (".sql", ".sas")
    assert source_suffixes("tsql") == (".sql", ".sas")
    assert source_suffixes("sas") == (".sas",)


@pytest.mark.parametrize(
    ("name", "dialect", "expected"),
    [
        ("q.sql", "tsql", "tsql"),
        ("q.sql", None, None),
        ("q.sas", "tsql", "sas"),
        ("q.SAS", None, "sas"),
    ],
)
def test_a_sas_file_is_always_a_sas_program(
    name: str, dialect: str | None, expected: str | None
) -> None:
    assert dialect_of(PurePosixPath(name), dialect) == expected


@pytest.mark.parametrize(
    ("outputs", "expected"),
    [
        (["a.py", "b.py", "c/a.py"], set()),
        (["a.py", "A.py", "b.py"], {0, 1}),
        (["a.py", "a.py/b.py", "c.py"], {0, 1}),
        (["A.py", "a.PY/b/c.py"], {0, 1}),
        # Whole components: a.py_extra is not inside a.py.
        (["a.py", "a.py_extra/b.py"], set()),
    ],
)
def test_outputs_collide_ignoring_case(outputs: list[str], expected: set[int]) -> None:
    assert colliding([PurePosixPath(output) for output in outputs]) == expected
