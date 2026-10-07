from sparkshift.diagnostics import Diagnostic
from sparkshift.errors import UnsupportedSQLError


def test_diagnostic_text_includes_the_sql_fragment() -> None:
    assert str(Diagnostic("WHERE clause", "WHERE a > 1")) == "WHERE clause: WHERE a > 1"


def test_diagnostic_text_includes_the_hint() -> None:
    diagnostic = Diagnostic("DELETE statement", "DELETE FROM t", hint="Use SELECT.")

    assert str(diagnostic) == "DELETE statement: DELETE FROM t. Hint: Use SELECT."


def test_long_sql_is_shortened_in_text_but_kept_in_full_as_data() -> None:
    sql = "WITH x AS (" + "SELECT 1 UNION ALL " * 10 + "SELECT 2)"
    diagnostic = Diagnostic("WITH clause", sql)

    text = str(diagnostic)
    assert len(text) == len("WITH clause: ") + 80
    assert text.endswith("...")
    assert diagnostic.sql == sql


def test_unsupported_error_message_uses_singular_for_one_issue() -> None:
    error = UnsupportedSQLError([Diagnostic("DISTINCT", "DISTINCT")])

    assert str(error) == "1 unsupported construct:\n  - DISTINCT: DISTINCT"
