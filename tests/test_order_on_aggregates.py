"""Fail-loud ordering on aggregate aliases — SPEC § 9."""

from __future__ import annotations

import pytest

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateOp,
    compute_aggregation,
    parse_aggregate_order,
)
from strawberry_django_aggregates.errors import OrderFieldNotAllowed


@pytest.mark.django_db
def test_order_by_aggregate_alias(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
        ],
        order_by=[("sum_total", "desc", None)],
    )
    sums = [r["sum_total"] for r in rows]
    # Decimal totals 700, 350, 75 in descending order.
    assert sums == sorted(sums, reverse=True)
    assert sums[0] == max(sums)


def test_native_grouping_discards_incoming_queryset_ordering(sample_orders):
    from tests.models import Order

    qs = Order.objects.order_by("pk")
    spec = [("customer", None)]
    requested = [(AggregateOp.COUNT, None)]
    rows = compute_aggregation(qs, group_by=spec, aggregates=requested)
    assert sorted(rows, key=lambda row: row["customer_id"]) == [
        {"customer_id": customer.pk, "count": count}
        for customer, count in zip(sample_orders[0], [3, 2, 1], strict=True)
    ]
    builder = AggregateBuilder(
        model=Order, aggregate_fields=["id"], group_by_fields=["customer"],
    )
    assert len(rows) == builder.count_groups(qs, spec, requested, {}) == 3


def test_ungrouped_aggregate_preserves_ordered_source_slice(sample_orders):
    from tests.models import Order

    qs = Order.objects.order_by("-pk")[:2]
    rows = compute_aggregation(
        qs, aggregates=[(AggregateOp.COUNT, None), (AggregateOp.SUM, "total")],
    )
    assert rows == [{"count": 2, "sum_total": 475}]
    assert qs.query.order_by == ("-pk",)


@pytest.mark.django_db
def test_order_unknown_alias_raises(sample_orders):
    from tests.models import Order

    with pytest.raises(OrderFieldNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            order_by=[("xyz_unknown", "desc", None)],
        )


def test_parse_django_flavor_descending():
    canonical, direction = parse_aggregate_order(
        "-sum_total",
        group_by_fields=["customer_id"],
        aggregate_aliases=["count", "sum_total"],
    )
    assert (canonical, direction) == ("sum_total", "desc")


def test_parse_explicit_suffix():
    canonical, direction = parse_aggregate_order(
        "count desc",
        group_by_fields=["customer_id"],
        aggregate_aliases=["count"],
    )
    assert (canonical, direction) == ("count", "desc")


def test_parse_odoo_flavor_field_colon_op():
    canonical, direction = parse_aggregate_order(
        "total:sum",
        group_by_fields=["customer_id"],
        aggregate_aliases=["sum_total"],
    )
    assert (canonical, direction) == ("sum_total", "asc")


def test_parse_bucketed_groupby_reference():
    canonical, direction = parse_aggregate_order(
        "created_at:month",
        group_by_fields=["created_at_month"],
        aggregate_aliases=[],
    )
    assert (canonical, direction) == ("created_at_month", "asc")


def test_parse_unknown_term_raises():
    with pytest.raises(OrderFieldNotAllowed):
        parse_aggregate_order(
            "nonexistent",
            group_by_fields=["customer_id"],
            aggregate_aliases=["count"],
        )


def test_parse_field_allowlist():
    canonical, direction = parse_aggregate_order(
        "name",
        group_by_fields=[],
        aggregate_aliases=[],
        field_allowlist=["name"],
    )
    assert (canonical, direction) == ("name", "asc")


@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("nulls", ["first", "last"])
def test_order_and_page_by_projected_relation_key(
    relation_key_case, direction, nulls,
):
    case = relation_key_case
    expected = case["rows"] if nulls == "last" else case["rows"][::-1]
    for offset, row in enumerate(expected):
        rows = compute_aggregation(
            case["queryset"].order_by("pk"),
            group_by=[(case["path"], None)],
            aggregates=[(AggregateOp.COUNT, None)],
            group_by_expressions=case["expressions"],
            order_by=[(case["alias"], direction, nulls)],
            respect_comodel_ordering=True,
            offset=offset,
            limit=1,
        )
        assert rows == [row]


def test_order_by_measure_with_relation_key_override(sample_orders):
    from django.db import models

    from tests.models import Order

    alpha = sample_orders[0][0]
    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.SUM, "total")],
        group_by_expressions={
            "customer": models.Case(
                models.When(customer=alpha, then=models.F("customer")),
                default=models.Value(None),
                output_field=models.BigIntegerField(),
            ),
        },
        order_by=[("sum_total", "asc", None)],
    )
    assert [row["customer_id"] for row in rows] == [None, alpha.pk]
    assert [row["sum_total"] for row in rows] == [425, 700]


def test_order_by_internal_key_alias_is_rejected(sample_orders):
    from django.db import models

    from tests.models import Order

    with pytest.raises(OrderFieldNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            group_by_expressions={"customer": models.F("customer")},
            order_by=[("_sda_customer_id", "asc", None)],
        )


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_order_uses_expression_values_instead_of_original_keys(
    sample_orders, direction,
):
    from django.db import models

    from tests.models import Order

    customers = sample_orders[0]
    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        group_by_expressions={"customer": 100 - models.F("customer")},
        order_by=[("customer_id", direction, None)],
    )
    expected = sorted(
        [100 - customer.pk for customer in customers],
        reverse=direction == "desc",
    )
    assert [row["customer_id"] for row in rows] == expected
