"""Name hygiene and symbolic IR references, without a Spark dependency."""

import pytest

from sparkshift import ir


@pytest.mark.parametrize("occupied", ["_IF_1", "_\u0131f_1", "_\u0130f_1"])
def test_static_allocation_protects_case_variants(occupied: str) -> None:
    assert ir.unique_column_name("_if_1", [occupied, "_IF_1_2"]) == "_if_1_3"


def test_expression_rewrites_do_not_rewrite_a_helper_binding() -> None:
    name = ir.HelperName("_if_1")
    expression = ir.Alias(ir.HelperColumn(name), name)

    assert ir.children(expression) == [ir.HelperColumn(name)]
    assert ir.map_children(expression, lambda child: ir.Literal(1)) == ir.Alias(
        ir.Literal(1), name
    )


def test_distinct_query_scopes_have_distinct_helper_identities() -> None:
    first, second = ir.HelperName("_qualify_1", 1), ir.HelperName("_qualify_1", 2)

    assert first != second
    assert ir.helper_reference(first) == ir.HelperColumn(first)
    assert ir.helper_reference("_having_1") == ir.Column(("_having_1",))


def test_exists_skips_only_output_helper_cleanup_and_keeps_its_limit() -> None:
    from sparkshift.translate import _exists_query

    table = ir.TableScan(("t",))
    output = ir.Named(
        "subquery", ir.Limit(ir.Distinct(ir.DropColumns(table, ("_qualify_1",))), 0)
    )
    assert _exists_query(output) == ir.Named("subquery", ir.Limit(table, 0))
    # A drop below a projection can affect expressions that must still resolve.
    projected = ir.Project(ir.DropColumns(table, ("user_column",)), (ir.Star(),))
    assert _exists_query(projected) == projected
