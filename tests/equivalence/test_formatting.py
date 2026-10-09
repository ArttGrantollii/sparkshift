"""Formatting checks over the generated code of every equivalence scenario.

Wrapping long expressions must only change line breaks and grouping
parentheses, never the code's meaning. These checks need no Spark: they
compare Python syntax trees and line lengths.
"""

import ast
from pathlib import Path

import pytest
import test_aggregation
import test_conditionals
import test_ctes
import test_dates
import test_expressions
import test_filtering
import test_functions
import test_joins
import test_ordering
import test_set_operations
import test_windows

from sparkshift.emit import emit
from sparkshift.parsing import parse_sql
from sparkshift.translate import translate

MODULES = [
    test_expressions,
    test_filtering,
    test_joins,
    test_aggregation,
    test_conditionals,
    test_functions,
    test_dates,
    test_ordering,
    test_windows,
    test_ctes,
    test_set_operations,
]


def _sql(scenario: str | tuple[str, ...]) -> str:
    """A scenario is its SQL, or a tuple that starts with it (followed by
    settings such as ordering keys)."""
    return scenario if isinstance(scenario, str) else scenario[0]


CASES = [
    (None, _sql(scenario))
    for module in MODULES
    for scenario in module.SCENARIOS.values()
] + [
    (dialect, sql)
    for module in MODULES
    for dialect, sql, *_ in getattr(module, "DIALECT_SCENARIOS", {}).values()
]


@pytest.mark.parametrize(("dialect", "sql"), CASES)
def test_wrapping_keeps_the_syntax_tree(dialect: str | None, sql: str) -> None:
    plan = translate(parse_sql(sql, dialect), dialect)
    one_line = ast.dump(ast.parse(emit(plan, line_length=10**6)))

    for line_length in (88, 30):
        assert ast.dump(ast.parse(emit(plan, line_length=line_length))) == one_line


@pytest.mark.parametrize(("dialect", "sql"), CASES)
def test_generated_lines_fit_the_line_length(dialect: str | None, sql: str) -> None:
    code = emit(translate(parse_sql(sql, dialect), dialect))

    too_long = [line for line in code.splitlines() if len(line) > 88]
    assert too_long == []


def test_formatting_covers_every_equivalence_module() -> None:
    # Fails if a module with equivalence cases is added but not listed here.
    with_cases = {
        path.stem
        for path in Path(__file__).parent.glob("test_*.py")
        if "\nSCENARIOS = {" in path.read_text()
    }
    assert with_cases == {module.__name__ for module in MODULES}
