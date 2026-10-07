import pytest

from sparkshift.dialects import SUPPORTED_DIALECTS, resolve_dialect
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
    assert caught.value.supported == SUPPORTED_DIALECTS
    for name in SUPPORTED_DIALECTS:
        assert name in str(caught.value)
