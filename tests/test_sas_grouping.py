"""SAS GROUP BY without a summary sorts rows; it must not deduplicate them."""

import pytest

from sparkshift import convert
from sparkshift.errors import UnsupportedSQLError


@pytest.mark.parametrize(
    "query",
    [
        "select customer_id from customers group by customer_id",
        "select distinct customer_id from customers group by customer_id",
        "select customer_id from customers group by customer_id order by customer_id",
        "select customer_id from customers group by customer_id having customer_id > 1",
        "select sum(customer_id, 1) as value from customers group by customer_id",
        "select customer_id from customers group by customer_id "
        "having sum(customer_id, 1) > 1",
    ],
)
def test_no_summary_group_by_is_rejected_with_rewrite_guidance(query: str) -> None:
    with pytest.raises(UnsupportedSQLError) as caught:
        convert(f"proc sql; create table out as {query}; quit;", dialect="sas")
    assert "GROUP BY without a summary function" in str(caught.value)
    assert "ORDER BY" in str(caught.value)
    assert "DISTINCT" in str(caught.value)


def test_summary_in_having_is_still_aggregation() -> None:
    result = convert(
        "proc sql; create table out as select customer_id from customers "
        "group by customer_id having count(*) > 1; quit;",
        dialect="sas",
    )
    assert ".groupBy(" in result.code
    assert "F.count(" in result.code


def test_generic_sql_group_by_without_summary_is_unchanged() -> None:
    result = convert("SELECT customer_id FROM customers GROUP BY customer_id")
    assert result.code
    assert result.warnings == ()
