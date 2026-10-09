"""Keep the README's examples identical to what SparkShift actually produces."""

from pathlib import Path

import pytest

import sparkshift

README = (Path(__file__).parents[1] / "README.md").read_text()


def test_usage_example_output_matches_real_output() -> None:
    sql = "SELECT order_id, amount * 1.10 AS with_tax FROM sales.orders"

    assert f'"{sql}"' in README
    assert f"```python\n{sparkshift.convert(sql).code}```" in README


def test_unsupported_example_matches_real_error() -> None:
    with pytest.raises(sparkshift.UnsupportedSQLError) as caught:
        sparkshift.convert(
            "SELECT name, ROW_NUMBER() OVER (ORDER BY name) AS rn FROM customers "
            "LIMIT 10 OFFSET 20"
        )

    assert f"UnsupportedSQLError: {caught.value}" in README
