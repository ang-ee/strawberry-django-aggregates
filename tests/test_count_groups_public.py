"""Public, database-side grouped cardinality."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from strawberry_django_aggregates import AggregateBuilder, AggregateOp
from strawberry_django_aggregates.errors import (
    AggregateError,
    GroupByFieldNotAllowed,
    OperatorNotSupportedError,
)


@pytest.fixture
def order_builder():
    from tests.models import Order

    return AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["customer", "status", "created_at"],
        json_paths={"metadata.region": "str"},
    )


@pytest.mark.django_db
def test_count_groups_counts_filtered_distinct_buckets(
    sample_orders, order_builder
):
    from tests.models import Order

    assert (
        order_builder.count_groups(
            Order.objects.filter(status="paid"),
            [("customer", None)],
            [(AggregateOp.COUNT, None)],
            {},
        )
        == 2
    )


@pytest.mark.django_db
def test_count_groups_counts_post_having_buckets(sample_orders, order_builder):
    from tests.models import Order

    assert (
        order_builder.count_groups(
            Order.objects.all(),
            [("customer", None)],
            [(AggregateOp.COUNT, None), (AggregateOp.SUM, "total")],
            {"sum_total__gt": Decimal("300.00")},
        )
        == 2
    )


@pytest.mark.django_db
def test_count_groups_uses_json_path(sample_orders, order_builder):
    from tests.models import Order

    first = sample_orders[1][0]
    Order.objects.filter(pk=first.pk).update(metadata={"region": "eu"})
    Order.objects.exclude(pk=first.pk).update(metadata={"region": "us"})

    assert (
        order_builder.count_groups(
            Order.objects.all(),
            [("metadata.region", None)],
            [(AggregateOp.COUNT, None)],
            {},
        )
        == 2
    )


@pytest.mark.django_db
def test_count_groups_uses_date_granularity(sample_orders, order_builder):
    from strawberry_django_aggregates import TimeGranularity
    from tests.models import Order

    assert (
        order_builder.count_groups(
            Order.objects.all(),
            [("created_at", TimeGranularity.MONTH)],
            [(AggregateOp.COUNT, None)],
            {},
        )
        == 2
    )


@pytest.mark.django_db
def test_count_groups_uses_the_requested_timezone(
    sample_orders, order_builder
):
    from strawberry_django_aggregates import TimeGranularity
    from tests.models import Order

    first, second = sample_orders[1][:2]
    Order.objects.filter(pk=first.pk).update(
        created_at=datetime.datetime(2026, 5, 1, 0, 30, tzinfo=datetime.UTC)
    )
    Order.objects.filter(pk=second.pk).update(
        created_at=datetime.datetime(2026, 5, 1, 8, 30, tzinfo=datetime.UTC)
    )
    scoped = Order.objects.filter(pk__in=(first.pk, second.pk))

    assert (
        order_builder.count_groups(
            scoped,
            [("created_at", TimeGranularity.DAY)],
            [(AggregateOp.COUNT, None)],
            {},
            tz="America/Los_Angeles",
        )
        == 2
    )


@pytest.mark.django_db
def test_count_groups_rejects_postgres_only_having_before_sql(
    sample_orders, order_builder
):
    from tests.models import Order

    with pytest.raises(OperatorNotSupportedError):
        order_builder.count_groups(
            Order.objects.all(),
            [("customer", None)],
            [(AggregateOp.STDDEV, "total")],
            {"stddev_total__gt": 0},
        )


@pytest.mark.django_db
def test_count_groups_validates_week_start(sample_orders, order_builder):
    from strawberry_django_aggregates import TimeGranularity
    from tests.models import Order

    with pytest.raises(ValueError, match="week_start"):
        order_builder.count_groups(
            Order.objects.all(),
            [("created_at", TimeGranularity.WEEK)],
            [(AggregateOp.COUNT, None)],
            {},
            week_start=0,
        )


@pytest.mark.django_db
def test_count_groups_rejects_having_without_groups(
    sample_orders, order_builder
):
    from tests.models import Order

    with pytest.raises(AggregateError, match="HAVING requires"):
        order_builder.count_groups(
            Order.objects.all(),
            [],
            [(AggregateOp.COUNT, None)],
            {"count__gt": 0},
        )


@pytest.mark.django_db
def test_count_groups_rejects_an_unselected_expression(
    sample_orders, order_builder
):
    from django.db import models

    from tests.models import Order

    with pytest.raises(GroupByFieldNotAllowed, match="unselected"):
        order_builder.count_groups(
            Order.objects.all(),
            [("status", None)],
            [(AggregateOp.COUNT, None)],
            {},
            group_by_expressions={"customer__name": models.Value(None)},
        )


@pytest.mark.django_db
def test_count_groups_rejects_expressions_without_grouping(order_builder):
    from django.db import models

    from tests.models import Order

    with pytest.raises(GroupByFieldNotAllowed, match="unselected"):
        order_builder.count_groups(
            Order.objects.all(),
            [],
            [(AggregateOp.COUNT, None)],
            {},
            group_by_expressions={"customer__name": models.Value(None)},
        )


@pytest.mark.parametrize("having", [{}, {"count__gt": 2}, {"count__gt": 99}])
def test_count_groups_with_relation_key_override(
    relation_key_case, having, django_assert_num_queries,
):
    case = relation_key_case
    builder = AggregateBuilder(
        model=case["queryset"].model,
        aggregate_fields=["id"],
        group_by_fields=[case["path"]],
    )
    expected = sum(
        row["count"] > having.get("count__gt", 0) for row in case["rows"]
    )
    with django_assert_num_queries(1):
        count = builder.count_groups(
            case["queryset"].order_by("pk"),
            [(case["path"], None)],
            [(AggregateOp.COUNT, None)],
            having,
            group_by_expressions=case["expressions"],
        )
    assert count == expected


@pytest.mark.parametrize(
    ("path", "value"),
    [("customer", None), ("customer", 17), ("customer__name", "x")],
)
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("having", [{}, {"count__gte": 0}, {"count__gt": 6}])
def test_constant_group_expression_rows_and_count_agree(
    sample_orders, order_builder, path, value, empty, having,
):
    from django.db import models

    from strawberry_django_aggregates import compute_aggregation
    from tests.models import Order

    qs = Order.objects.filter(pk=-1) if empty else Order.objects.all()
    spec = [(path, None)]
    requested = [(AggregateOp.COUNT, None)]
    alias = "customer_id" if path == "customer" else path
    output_field = (
        models.BigIntegerField() if path == "customer" else models.CharField()
    )
    expressions = {
        path: models.Value(value, output_field=output_field),
    }
    rows = compute_aggregation(
        qs, group_by=spec, aggregates=requested, having=having,
        group_by_expressions=expressions,
    )
    expected = (
        [] if empty or "count__gt" in having
        else [{alias: value, "count": 6}]
    )
    assert rows == expected
    assert order_builder.count_groups(
        qs, spec, requested, having, group_by_expressions=expressions,
    ) == len(expected)
