"""Star modifiers must never turn into an unchanged wildcard silently."""

import io
import json
import runpy
from pathlib import Path

import pytest
from sqlglot import exp

import sparkshift
from sparkshift.cli import EXIT_NOT_CONVERTED, main
from sparkshift.parsing import parse_sql
from sparkshift.translate import translate


@pytest.mark.parametrize("qualifier", ["", "t."])
@pytest.mark.parametrize(
    ("dialect", "modifier", "label"),
    [
        (None, "EXCEPT (secret)", "EXCEPT"),
        ("bigquery", "EXCEPT (secret)", "EXCEPT"),
        ("bigquery", "REPLACE (amount * 2 AS amount)", "REPLACE"),
        ("snowflake", "EXCLUDE (secret)", "EXCLUDE"),
        ("snowflake", "REPLACE (amount * 2 AS amount)", "REPLACE"),
        ("snowflake", "RENAME amount AS total", "RENAME"),
        ("snowflake", "ILIKE 'amount%'", "ILIKE"),
    ],
)
def test_projection_modifiers_are_rejected_with_rewrite_guidance(
    qualifier: str, dialect: str | None, modifier: str, label: str
) -> None:
    sql = f"SELECT {qualifier}* {modifier} FROM audit_input t"
    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        sparkshift.convert(sql, dialect=dialect)

    [issue] = caught.value.issues
    assert issue.message == f"SELECT * with {label}"
    parsed_star = parse_sql(sql, dialect).find(exp.Star)
    assert parsed_star is not None
    assert issue.sql == parsed_star.sql(dialect=dialect)
    assert issue.hint is not None
    assert "explicit" in issue.hint.lower()


@pytest.mark.parametrize("window", ["", " OVER (PARTITION BY id)"])
@pytest.mark.parametrize(
    ("modifier", "label"),
    [("EXCLUDE secret", "EXCLUDE"), ("ILIKE 'amount%'", "ILIKE")],
)
def test_count_star_modifiers_are_not_replaced_by_row_counts(
    modifier: str, label: str, window: str
) -> None:
    sql = f"SELECT COUNT(* {modifier}){window} AS n FROM audit_input"
    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        sparkshift.convert(sql, dialect="snowflake")

    [issue] = caught.value.issues
    assert issue.message == f"COUNT(*) with {label}"
    assert issue.hint is not None
    assert "NULL" in issue.hint


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT * EXCEPT (secret) FROM t) SELECT * FROM x",
        "SELECT id FROM (SELECT * EXCEPT (secret) FROM t) x",
        "SELECT * FROM t UNION ALL SELECT * EXCEPT (secret) FROM t",
        "SELECT t.* EXCEPT (secret) FROM t JOIN u ON t.id = u.id",
    ],
)
def test_modifiers_in_composed_queries_are_rejected(sql: str) -> None:
    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        sparkshift.convert(sql)

    assert any(issue.message == "SELECT * with EXCEPT" for issue in caught.value.issues)


def test_combined_modifiers_and_other_issues_are_all_reported() -> None:
    sql = (
        "SELECT * EXCEPT (secret) REPLACE (amount * 2 AS amount), "
        "my_udf(id) AS x FROM t LIMIT 3 OFFSET 1"
    )
    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        sparkshift.convert(sql, dialect="bigquery")

    assert {issue.message for issue in caught.value.issues} == {
        "SELECT * with EXCEPT",
        "SELECT * with REPLACE",
        "function MY_UDF",
        "OFFSET clause",
    }


@pytest.mark.parametrize(
    "sql", ["SELECT * FROM t", "SELECT t.* FROM t", "SELECT COUNT(*) AS n FROM t"]
)
def test_unknown_future_star_modifier_fails_closed(sql: str) -> None:
    tree = parse_sql(sql)
    star = tree.find(exp.Star)
    assert star is not None
    star.set("future_modifier", exp.Literal.string("meaningful"))

    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        translate(tree)

    assert "FUTURE_MODIFIER" in caught.value.issues[0].message


def test_cli_rejection_writes_no_generated_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("SELECT * EXCEPT (secret) FROM t"))

    assert main(["convert", "-"]) == EXIT_NOT_CONVERTED
    out, err = capsys.readouterr()
    assert out == ""
    assert "SELECT * with EXCEPT" in err


def test_playground_bridge_returns_an_error_instead_of_unchanged_code() -> None:
    bridge = runpy.run_path(str(Path(__file__).parents[1] / "playground/bridge.py"))
    result = json.loads(
        bridge["convert_for_page"]("SELECT * EXCEPT (secret) FROM t", "")
    )

    assert result["ok"] is False
    assert "code" not in result
    assert result["issues"][0]["message"] == "SELECT * with EXCEPT"
    assert result["issues"][0]["hint"] is not None
