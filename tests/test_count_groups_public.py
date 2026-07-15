"""Public, database-side grouped cardinality."""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from strawberry_django_aggregates import AggregateBuilder, AggregateOp
from strawberry_django_aggregates.errors import (
    AggregateError,
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
