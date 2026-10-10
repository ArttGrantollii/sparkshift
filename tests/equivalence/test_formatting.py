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
import test_qualify
import test_set_operations
import test_subqueries
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
    test_qualify,
    test_ctes,
    test_set_operations,
    test_subqueries,
]


def _sql(scenario: str | tuple[str, ...]) -> str:
    """A scenario is its SQL, or a tuple that starts with it (followed by
    settings such as ordering keys)."""
    return scenario if isinstance(scenario, str) else scenario[0]


# Queries written the way people paste them into the playground, with longer
# names than the test tables. Formatting only: their tables are not in the
# equivalence datasets.
SAMPLE_QUERIES = [
    (
        None,
        "SELECT region, COUNT(*) AS orders, SUM(amount) AS revenue, "
        "AVG(amount) AS avg_order FROM sales WHERE order_date >= DATE '2024-01-01' "
        "GROUP BY region HAVING SUM(amount) > 10000 ORDER BY revenue DESC",
    ),
    (
        "tsql",
        "SELECT TOP 10 c.customer_name, SUM(o.total) AS spent FROM dbo.customers c "
        "JOIN dbo.orders o ON o.customer_id = c.customer_id "
        "WHERE o.order_date >= DATEADD(day, -30, CAST(GETDATE() AS DATE)) "
        "GROUP BY c.customer_name ORDER BY spent DESC",
    ),
    (
        "postgres",
        "SELECT customer_id, order_date, amount, SUM(amount) OVER (PARTITION BY "
        "customer_id ORDER BY order_date ROWS BETWEEN UNBOUNDED PRECEDING AND "
        "CURRENT ROW) AS running_total, LAG(amount) OVER (PARTITION BY customer_id "
        "ORDER BY order_date) AS previous_amount FROM orders",
    ),
    (
        "snowflake",
        "WITH customer_spend AS (SELECT customer_id, SUM(amount) AS total "
        "FROM orders GROUP BY customer_id) SELECT customer_id, total, "
        "CASE WHEN total >= 1000 THEN 'gold' WHEN total >= 500 THEN 'silver' "
        "ELSE 'bronze' END AS tier FROM customer_spend",
    ),
    (
        "mysql",
        "SELECT p.product_name, p.price FROM products p WHERE NOT EXISTS "
        "(SELECT 1 FROM order_items oi WHERE oi.product_id = p.product_id) "
        "ORDER BY p.price DESC LIMIT 5",
    ),
    (
        "oracle",
        "SELECT employee_id, NVL(commission, 0) AS commission, salary "
        "FROM employees WHERE department_id IN (SELECT department_id "
        "FROM departments WHERE location = 'London') "
        "ORDER BY salary DESC FETCH FIRST 3 ROWS ONLY",
    ),
    (
        None,
        "SELECT invoice_id FROM invoices WHERE invoice_total_amount * "
        "(1 - customer_discount_rate) + shipping_cost_amount - loyalty_credit_amount "
        "> minimum_billable_amount_for_region * exchange_rate_to_euro",
    ),
]

CASES = (
    [
        (None, _sql(scenario))
        for module in MODULES
        for scenario in module.SCENARIOS.values()
    ]
    + [
        (dialect, sql)
        for module in MODULES
        for dialect, sql, *_ in getattr(module, "DIALECT_SCENARIOS", {}).values()
    ]
    + SAMPLE_QUERIES
)


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
