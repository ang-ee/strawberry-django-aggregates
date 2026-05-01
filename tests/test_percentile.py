"""PERCENTILE_CONT / PERCENTILE_DISC / MODE — SPEC § 5 + § 5.1.

The ordered-set aggregates are PG-only — these tests skip on SQLite.

PG behaviour we pin:

- ``PERCENTILE_CONT(0.5)`` over an evenly-spaced 10-row distribution is
  the linear interpolation midpoint.
- ``PERCENTILE_CONT(0.9)`` falls between rows 9 and 10 and interpolates.
- ``PERCENTILE_DISC(0.5)`` returns an actual row value (no interpolation).
- ``MODE()`` returns the most-frequent value.

The fraction-encoded alias scheme is also pinned via assertion on the
returned row keys (``percentile_cont_total_50`` / ``..._90``).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection

from strawberry_django_aggregates import (
    AggregateOp,
    compute_aggregation,
)
from strawberry_django_aggregates.compiler import aggregate_alias

pytestmark = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="PERCENTILE_CONT / PERCENTILE_DISC / MODE are Postgres-only.",
)


@pytest.fixture
def percentile_orders(db):
    """Ten orders with totals 10, 20, 30, ..., 100 — easy P50/P90 math."""
    import datetime

    from tests.models import Customer, Order

    customer = Customer.objects.create(name="P-Customer")
    tz = datetime.UTC
    base = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=tz)
    orders = []
    for i in range(1, 11):
        orders.append(Order.objects.create(
            customer=customer,
            status="paid",
            total=Decimal(i * 10),
            quantity=1,
            is_priority=False,
            created_at=base + datetime.timedelta(days=i),
        ))
    return orders


@pytest.mark.django_db
def test_percentile_cont_p50(percentile_orders):
    """P50 over 10..100 evenly spaced: PG returns 55.0 (midpoint between
    50 and 60 — the interpolation between the 5th and 6th values).
    """
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[(AggregateOp.PERCENTILE_CONT, "total")],
        op_args={"percentile_cont_total": {"fraction": 0.5}},
    )
    assert len(rows) == 1
    expected_alias = aggregate_alias(
        AggregateOp.PERCENTILE_CONT, "total", fraction=0.5,
    )
    assert expected_alias == "percentile_cont_total_50"
    value = rows[0][expected_alias]
    assert float(value) == pytest.approx(55.0)


@pytest.mark.django_db
def test_percentile_cont_p90(percentile_orders):
    """P90 over 10..100 evenly spaced: PG returns 91.0 (interpolation
    between the 9th and 10th values, 90 and 100, at 0.1 of the way).
    """
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[(AggregateOp.PERCENTILE_CONT, "total")],
        op_args={"percentile_cont_total": {"fraction": 0.9}},
    )
    expected_alias = aggregate_alias(
        AggregateOp.PERCENTILE_CONT, "total", fraction=0.9,
    )
    assert expected_alias == "percentile_cont_total_90"
    value = rows[0][expected_alias]
    assert float(value) == pytest.approx(91.0)


@pytest.mark.django_db
def test_percentile_disc_p50(percentile_orders):
    """P50 (discrete) over 10..100: PG returns the 5th row value
    (50) — first row whose CDF meets the fraction.
    """
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[(AggregateOp.PERCENTILE_DISC, "total")],
        op_args={"percentile_disc_total": {"fraction": 0.5}},
    )
    expected_alias = aggregate_alias(
        AggregateOp.PERCENTILE_DISC, "total", fraction=0.5,
    )
    assert expected_alias == "percentile_disc_total_50"
    value = rows[0][expected_alias]
    assert float(value) == pytest.approx(50.0)


@pytest.mark.django_db
def test_mode_returns_most_frequent(db):
    """MODE() picks the most-frequent value. Stage one clearly-most-
    frequent value (300 appears 5x; everything else 1x) and assert.
    """
    import datetime

    from tests.models import Customer, Order

    customer = Customer.objects.create(name="M-Customer")
    tz = datetime.UTC
    base = datetime.datetime(2026, 5, 1, 12, 0, tzinfo=tz)
    # 5 orders at total=300 (the mode), then one each at 100/200/400/500.
    for i in range(5):
        Order.objects.create(
            customer=customer, status="paid",
            total=Decimal("300"), quantity=1, is_priority=False,
            created_at=base + datetime.timedelta(days=i),
        )
    for total in (100, 200, 400, 500):
        Order.objects.create(
            customer=customer, status="paid",
            total=Decimal(total), quantity=1, is_priority=False,
            created_at=base + datetime.timedelta(days=10 + total),
        )

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[(AggregateOp.MODE, "total")],
    )
    assert len(rows) == 1
    value = rows[0]["mode_total"]
    assert Decimal(value) == Decimal("300")


@pytest.mark.django_db
def test_percentile_fraction_validation(percentile_orders):
    """Out-of-range fractions raise ``ValueError`` before any SQL fires."""
    from tests.models import Order

    with pytest.raises(ValueError, match="fraction"):
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(AggregateOp.PERCENTILE_CONT, "total")],
            op_args={"percentile_cont_total": {"fraction": 1.5}},
        )

    with pytest.raises(ValueError, match="fraction"):
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(AggregateOp.PERCENTILE_CONT, "total")],
            op_args={"percentile_cont_total": {"fraction": -0.1}},
        )


@pytest.mark.django_db
def test_percentile_missing_op_args_raises(percentile_orders):
    """Forgetting to pass ``op_args`` for a percentile op fails loud."""
    from tests.models import Order

    with pytest.raises(ValueError, match="fraction"):
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(AggregateOp.PERCENTILE_CONT, "total")],
        )
