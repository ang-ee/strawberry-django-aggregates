"""HAVING with aggregate aliases — SPEC § 8."""

from __future__ import annotations

from decimal import Decimal

import pytest

from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
)
from strawberry_django_aggregates.errors import HavingFieldNotAllowed


@pytest.mark.django_db
def test_having_sum_total_gt(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
        ],
        having={"sum_total__gt": Decimal("300.00")},
    )
    # Only customer Alpha (700) and Beta (350) have sum_total > 300.
    sums = sorted(r["sum_total"] for r in rows)
    assert sums == [Decimal("350.00"), Decimal("700.00")]


@pytest.mark.django_db
def test_having_count_eq(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
        ],
        having={"count__eq": 3},
    )
    # Only customer Alpha has 3 orders.
    assert len(rows) == 1
    assert rows[0]["count"] == 3


@pytest.mark.django_db
def test_having_neq(sample_orders):
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        having={"count__neq": 3},
    )
    counts = sorted(r["count"] for r in rows)
    assert counts == [1, 2]


@pytest.mark.django_db
def test_having_unknown_alias_raises(sample_orders):
    from tests.models import Order

    with pytest.raises(HavingFieldNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            having={"sum_unknown_field__gt": Decimal("0")},
        )


@pytest.mark.django_db
def test_having_unknown_comparison_raises(sample_orders):
    from tests.models import Order

    with pytest.raises(HavingFieldNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            having={"count__between": 1},
        )


def test_having_filters_merged_relation_key_groups(relation_key_case):
    case = relation_key_case
    rows = compute_aggregation(
        case["queryset"],
        group_by=[(case["path"], None)],
        aggregates=[(AggregateOp.COUNT, None)],
        group_by_expressions=case["expressions"],
        having={"count__gt": 2},
        order_by=[(case["alias"], "asc", "last")],
    )
    assert rows == [row for row in case["rows"] if row["count"] > 2]


def test_having_sum_uses_all_rows_in_projected_null_group(sample_orders):
    from django.db import models

    from tests.models import Order

    alpha = sample_orders[0][0]
    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer", None)],
        aggregates=[(AggregateOp.COUNT, None), (AggregateOp.SUM, "total")],
        group_by_expressions={
            "customer": models.Case(
                models.When(customer=alpha, then=models.F("customer")),
                default=models.Value(None),
                output_field=models.BigIntegerField(),
            ),
        },
        having={"sum_total__gt": 400, "sum_total__lt": 500},
    )
    assert rows == [
        {"customer_id": None, "count": 3, "sum_total": Decimal("425.00")},
    ]
