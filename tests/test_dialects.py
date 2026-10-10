import pytest

from sparkshift.dialects import (
    DIALECT_CHOICES,
    SAS,
    SUPPORTED_DIALECTS,
    is_sas,
    resolve_dialect,
)
from sparkshift.errors import UnsupportedDialectError


def test_none_selects_the_generic_dialect() -> None:
    assert resolve_dialect(None) is None


@pytest.mark.parametrize("dialect", SUPPORTED_DIALECTS)
def test_supported_dialects_resolve_to_themselves(dialect: str) -> None:
    assert resolve_dialect(dialect) == dialect


@pytest.mark.parametrize(
    ("given", "expected"),
    [("TSQL", "tsql"), (" Snowflake ", "snowflake"), ("BigQuery", "bigquery")],
)
def test_dialect_names_are_case_and_whitespace_insensitive(
    given: str, expected: str
) -> None:
    assert resolve_dialect(given) == expected


@pytest.mark.parametrize("dialect", ["sqlserver", "postgresql", ""])
def test_unknown_dialect_is_rejected_with_the_supported_list(dialect: str) -> None:
    with pytest.raises(UnsupportedDialectError) as caught:
        resolve_dialect(dialect)

    assert caught.value.dialect == dialect
    assert caught.value.supported == DIALECT_CHOICES
    for name in DIALECT_CHOICES:
        assert name in str(caught.value)


def test_sas_is_a_choice_but_not_a_sql_dialect() -> None:
    assert (*SUPPORTED_DIALECTS, SAS) == DIALECT_CHOICES
    assert SAS not in SUPPORTED_DIALECTS


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [("sas", True), (" SAS ", True), (None, False), ("tsql", False)],
)
def test_is_sas(dialect: str | None, expected: bool) -> None:
    assert is_sas(dialect) is expected
