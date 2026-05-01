"""Stream 7 — empty-bucket filling for date-bucketed aggregations.

Three layers of coverage:

1. Direct unit tests of :func:`generate_bucket_spine` and
   :func:`fill_bucket_results` — pure-stdlib, no DB.
2. SQL-level tests through :func:`compute_aggregation` — sparse-month
   data with ``fill=True`` produces dense bucketing.
3. GraphQL surface — ``fill: Boolean``, ``fillMin: DateTime``,
   ``fillMax: DateTime`` resolver args.
4. SDL determinism — args appear unconditionally, byte-identical
   across two builds.

No ``from __future__ import annotations`` — strawberry resolves Query
field-type annotations against ``__globals__`` and PEP 563 turns
dynamic types into unevaluable strings. Mirrors the pattern in
``test_bucket_range.py`` and ``test_week_start.py``.
"""

import datetime
from decimal import Decimal

import pytest
import strawberry

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateError,
    AggregateOp,
    NumberGranularity,
    TimeGranularity,
    compute_aggregation,
    fill_bucket_results,
    generate_bucket_spine,
)

UTC = datetime.UTC


# ---------------------------------------------------------------------------
# 1 · generate_bucket_spine — direct unit tests
# ---------------------------------------------------------------------------


def test_spine_month_inclusive_endpoints() -> None:
    spine = list(generate_bucket_spine(
        datetime.datetime(2026, 1, 1, tzinfo=UTC),
        datetime.datetime(2026, 6, 1, tzinfo=UTC),
        TimeGranularity.MONTH,
    ))
    assert spine == [
        datetime.datetime(2026, 1, 1, tzinfo=UTC),
        datetime.datetime(2026, 2, 1, tzinfo=UTC),
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
        datetime.datetime(2026, 4, 1, tzinfo=UTC),
        datetime.datetime(2026, 5, 1, tzinfo=UTC),
        datetime.datetime(2026, 6, 1, tzinfo=UTC),
    ]


def test_spine_month_floors_unaligned_endpoints() -> None:
    """Non-bucket-aligned endpoints are floored — ``Jan 15`` floors to
    ``Jan 1`` for MONTH bucketing."""
    spine = list(generate_bucket_spine(
        datetime.datetime(2026, 1, 15, 12, 30, tzinfo=UTC),
        datetime.datetime(2026, 3, 17, 8, 0, tzinfo=UTC),
        TimeGranularity.MONTH,
    ))
    assert spine == [
        datetime.datetime(2026, 1, 1, tzinfo=UTC),
        datetime.datetime(2026, 2, 1, tzinfo=UTC),
        datetime.datetime(2026, 3, 1, tzinfo=UTC),
    ]


def test_spine_day_grain() -> None:
    spine = list(generate_bucket_spine(
        datetime.datetime(2026, 5, 1, tzinfo=UTC),
        datetime.datetime(2026, 5, 4, tzinfo=UTC),
        TimeGranularity.DAY,
    ))
    assert spine == [
        datetime.datetime(2026, 5, 1, tzinfo=UTC),
        datetime.datetime(2026, 5, 2, tzinfo=UTC),
        datetime.datetime(2026, 5, 3, tzinfo=UTC),
        datetime.datetime(2026, 5, 4, tzinfo=UTC),
    ]


def test_spine_quarter_grain() -> None:
    spine = list(generate_bucket_spine(
        datetime.datetime(2026, 1, 1, tzinfo=UTC),
        datetime.datetime(2027, 1, 1, tzinfo=UTC),
        TimeGranularity.QUARTER,
    ))
    assert spine == [
        datetime.datetime(2026, 1, 1, tzinfo=UTC),
        datetime.datetime(2026, 4, 1, tzinfo=UTC),
        datetime.datetime(2026, 7, 1, tzinfo=UTC),
        datetime.datetime(2026, 10, 1, tzinfo=UTC),
        datetime.datetime(2027, 1, 1, tzinfo=UTC),
    ]


def test_spine_week_default_iso() -> None:
    """Mon May 4 → start of week; +7 days → next Mon."""
    spine = list(generate_bucket_spine(
        datetime.datetime(2026, 5, 4, tzinfo=UTC),
        datetime.datetime(2026, 5, 18, tzinfo=UTC),
        TimeGranularity.WEEK,
    ))
    assert spine == [
        datetime.datetime(2026, 5, 4, tzinfo=UTC),
        datetime.datetime(2026, 5, 11, tzinfo=UTC),
        datetime.datetime(2026, 5, 18, tzinfo=UTC),
    ]


def test_spine_week_sunday_start() -> None:
    """``week_start=7`` — Sunday-first weeks. May 3 (Sun) → start."""
    spine = list(generate_bucket_spine(
        datetime.datetime(2026, 5, 3, tzinfo=UTC),
        datetime.datetime(2026, 5, 17, tzinfo=UTC),
        TimeGranularity.WEEK,
        week_start=7,
    ))
    assert spine == [
        datetime.datetime(2026, 5, 3, tzinfo=UTC),
        datetime.datetime(2026, 5, 10, tzinfo=UTC),
        datetime.datetime(2026, 5, 17, tzinfo=UTC),
    ]


def test_spine_empty_when_min_after_max() -> None:
    spine = list(generate_bucket_spine(
        datetime.datetime(2026, 6, 1, tzinfo=UTC),
        datetime.datetime(2026, 1, 1, tzinfo=UTC),
        TimeGranularity.MONTH,
    ))
    assert spine == []


def test_spine_tzinfo_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="tzinfo"):
        list(generate_bucket_spine(
            datetime.datetime(2026, 1, 1, tzinfo=UTC),
            datetime.datetime(2026, 6, 1),  # naive
            TimeGranularity.MONTH,
        ))


def test_spine_validates_week_start() -> None:
    with pytest.raises(ValueError, match="week_start"):
        list(generate_bucket_spine(
            datetime.datetime(2026, 5, 4, tzinfo=UTC),
            datetime.datetime(2026, 5, 18, tzinfo=UTC),
            TimeGranularity.WEEK,
            week_start=8,
        ))


# ---------------------------------------------------------------------------
# 2 · fill_bucket_results — direct unit tests
# ---------------------------------------------------------------------------


def _row(month: datetime.datetime, count: int, **extras) -> dict:
    base = {"created_at_month": month, "count": count}
    base.update(extras)
    return base


def test_fill_results_inserts_missing_months() -> None:
    """Three-row sparse Jan/Mar/Jun input → six-row dense Jan-Jun output."""
    rows = [
        _row(datetime.datetime(2026, 1, 1, tzinfo=UTC), 5, sum_total=100),
        _row(datetime.datetime(2026, 3, 1, tzinfo=UTC), 8, sum_total=200),
        _row(datetime.datetime(2026, 6, 1, tzinfo=UTC), 3, sum_total=50),
    ]
    out = fill_bucket_results(
        rows,
        group_by_spec=[("created_at", TimeGranularity.MONTH)],
        aggregate_aliases=["count", "sum_total"],
        fill_min=None, fill_max=None, week_start=1,
    )
    assert len(out) == 6
    counts = [r["count"] for r in out]
    assert counts == [5, 0, 8, 0, 0, 3]
    # Filled rows have measure = None.
    assert out[1]["sum_total"] is None
    assert out[3]["sum_total"] is None
    # Bucket alias stamped on every row.
    months = [r["created_at_month"].month for r in out]
    assert months == [1, 2, 3, 4, 5, 6]


def test_fill_results_with_explicit_min_max() -> None:
    """``fill_min``/``fill_max`` override the data-derived window."""
    rows = [
        _row(datetime.datetime(2026, 3, 1, tzinfo=UTC), 5),
    ]
    out = fill_bucket_results(
        rows,
        group_by_spec=[("created_at", TimeGranularity.MONTH)],
        aggregate_aliases=["count"],
        fill_min=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        fill_max=datetime.datetime(2026, 5, 1, tzinfo=UTC),
        week_start=1,
    )
    assert len(out) == 5
    counts = [r["count"] for r in out]
    assert counts == [0, 0, 5, 0, 0]


def test_fill_results_empty_input_with_explicit_bounds() -> None:
    """No data + explicit bounds → fully filled spine of zeros."""
    out = fill_bucket_results(
        rows=[],
        group_by_spec=[("created_at", TimeGranularity.MONTH)],
        aggregate_aliases=["count", "sum_total"],
        fill_min=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        fill_max=datetime.datetime(2026, 3, 1, tzinfo=UTC),
        week_start=1,
    )
    assert len(out) == 3
    assert all(r["count"] == 0 for r in out)
    assert all(r["sum_total"] is None for r in out)


def test_fill_results_empty_input_without_bounds_returns_empty() -> None:
    """No data and no bounds → no spine to generate; pass through."""
    out = fill_bucket_results(
        rows=[],
        group_by_spec=[("created_at", TimeGranularity.MONTH)],
        aggregate_aliases=["count"],
        fill_min=None, fill_max=None, week_start=1,
    )
    assert out == []


def test_fill_results_requires_time_granularity() -> None:
    """No TIME-granularity entry → ValueError (caller-level guard
    duplicated defensively)."""
    with pytest.raises(ValueError, match="TIME-granularity"):
        fill_bucket_results(
            rows=[],
            group_by_spec=[("status", None)],
            aggregate_aliases=["count"],
            fill_min=None, fill_max=None, week_start=1,
        )


def test_fill_results_rejects_multi_time_granularity() -> None:
    with pytest.raises(ValueError, match="TIME-granularity"):
        fill_bucket_results(
            rows=[],
            group_by_spec=[
                ("created_at", TimeGranularity.MONTH),
                ("updated_at", TimeGranularity.MONTH),
            ],
            aggregate_aliases=["count"],
            fill_min=None, fill_max=None, week_start=1,
        )


def test_fill_results_week_start_sunday() -> None:
    """``week_start=7`` — spine respects user week boundary."""
    rows = [
        _row(datetime.datetime(2026, 5, 3, tzinfo=UTC), 5),  # Sun
        _row(datetime.datetime(2026, 5, 17, tzinfo=UTC), 7),  # Sun
    ]
    out = fill_bucket_results(
        # The bucket alias is derived from ``f"{field}_{granularity.value}"``
        # which is ``"created_at_week"`` — same string regardless of
        # week_start; the value semantics differ but the alias doesn't.
        [{"created_at_week": r["created_at_month"], "count": r["count"]}
         for r in rows],
        group_by_spec=[("created_at", TimeGranularity.WEEK)],
        aggregate_aliases=["count"],
        fill_min=None, fill_max=None, week_start=7,
    )
    assert len(out) == 3  # Sun May 3, 10, 17
    counts = [r["count"] for r in out]
    assert counts == [5, 0, 7]
    weeks = [r["created_at_week"].day for r in out]
    assert weeks == [3, 10, 17]


# ---------------------------------------------------------------------------
# 3 · compute_aggregation — fill end-to-end
# ---------------------------------------------------------------------------


@pytest.fixture
def sparse_orders(db):
    """Orders in Jan, March, June 2026 only — Feb/April/May empty."""
    from tests.models import Customer, Order

    c = Customer.objects.create(name="Sparse")

    Order.objects.create(
        customer=c, status="paid", total=Decimal("100.00"), quantity=1,
        created_at=datetime.datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
    )
    Order.objects.create(
        customer=c, status="paid", total=Decimal("200.00"), quantity=1,
        created_at=datetime.datetime(2026, 3, 20, 12, 0, tzinfo=UTC),
    )
    Order.objects.create(
        customer=c, status="paid", total=Decimal("400.00"), quantity=2,
        created_at=datetime.datetime(2026, 6, 5, 12, 0, tzinfo=UTC),
    )
    return c


@pytest.mark.django_db
def test_fill_inserts_zero_count_rows_for_missing_months(
    sparse_orders,
) -> None:
    """Sparse Jan/Mar/Jun data → dense Jan..Jun output with count=0
    for the empty months."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[(AggregateOp.COUNT, None)],
        fill=True,
    )
    assert len(rows) == 6
    by_month = {r["created_at_month"].month: r["count"] for r in rows}
    assert by_month == {1: 1, 2: 0, 3: 1, 4: 0, 5: 0, 6: 1}


@pytest.mark.django_db
def test_fill_with_sum_measure_returns_null_for_filled_rows(
    sparse_orders,
) -> None:
    """Filled rows have count=0 and SUM=None (NOT 0 — see SPEC § 5
    NULL semantics for empty-group SUM)."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
        ],
        fill=True,
    )
    feb = next(r for r in rows if r["created_at_month"].month == 2)
    assert feb["count"] == 0
    assert feb["sum_total"] is None
    jan = next(r for r in rows if r["created_at_month"].month == 1)
    assert jan["count"] == 1
    assert jan["sum_total"] == Decimal("100.00")


@pytest.mark.django_db
def test_fill_with_explicit_bounds_extends_window(sparse_orders) -> None:
    """``fill_min`` / ``fill_max`` extend the spine beyond the data
    range."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[(AggregateOp.COUNT, None)],
        fill=True,
        fill_min=datetime.datetime(2025, 11, 1, tzinfo=UTC),
        fill_max=datetime.datetime(2026, 8, 1, tzinfo=UTC),
    )
    months = [
        (r["created_at_month"].year, r["created_at_month"].month)
        for r in rows
    ]
    assert months == [
        (2025, 11), (2025, 12),
        (2026, 1), (2026, 2), (2026, 3), (2026, 4),
        (2026, 5), (2026, 6), (2026, 7), (2026, 8),
    ]


@pytest.mark.django_db
def test_fill_having_filters_before_fill(sparse_orders) -> None:
    """HAVING applies BEFORE fill — filled zero-count buckets are NOT
    subject to HAVING-on-aggregate, but a HAVING-filtered actual row
    is removed and is NOT back-filled with a zero row.

    With sum_total > 150: only March (200) and June (400) survive
    HAVING. Fill then expands April/May (between March/June) but NOT
    January (which was filtered out)."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "total"),
        ],
        having={"sum_total__gt": Decimal("150.00")},
        fill=True,
    )
    by_month = {r["created_at_month"].month: r["count"] for r in rows}
    # March (post-HAVING data min) and June (data max) both present.
    # April + May filled with 0. January was filtered out by HAVING
    # and NOT back-filled.
    assert by_month == {3: 1, 4: 0, 5: 0, 6: 1}


@pytest.mark.django_db
def test_fill_default_order_ascending_by_bucket(sparse_orders) -> None:
    """Filled rows sorted ASC by bucket alias by default."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[(AggregateOp.COUNT, None)],
        fill=True,
    )
    months = [r["created_at_month"] for r in rows]
    assert months == sorted(months)


@pytest.mark.django_db
def test_fill_user_order_by_descending(sparse_orders) -> None:
    """Explicit ``order_by`` overrides default ASC."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[(AggregateOp.COUNT, None)],
        order_by=[("created_at_month", "desc", None)],
        fill=True,
    )
    months = [r["created_at_month"] for r in rows]
    assert months == sorted(months, reverse=True)


@pytest.mark.django_db
def test_fill_offset_and_limit_apply_after_fill(sparse_orders) -> None:
    """Pagination on the dense spine — ``limit=3`` returns 3 rows from
    the front of the filled list, NOT 3 actual data rows."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[(AggregateOp.COUNT, None)],
        fill=True,
        offset=1, limit=3,
    )
    # Window: Jan..Jun → 6 buckets. offset=1, limit=3 → Feb, Mar, Apr.
    months = [r["created_at_month"].month for r in rows]
    assert months == [2, 3, 4]


@pytest.mark.django_db
def test_fill_rejects_no_group_by(sparse_orders) -> None:
    from tests.models import Order

    with pytest.raises(AggregateError, match="TIME-granularity"):
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(AggregateOp.COUNT, None)],
            fill=True,
        )


@pytest.mark.django_db
def test_fill_rejects_multi_level_group_by(sparse_orders) -> None:
    from tests.models import Order

    with pytest.raises(AggregateError, match="multi-level"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[
                ("created_at", TimeGranularity.MONTH),
                ("status", None),
            ],
            aggregates=[(AggregateOp.COUNT, None)],
            fill=True,
        )


@pytest.mark.django_db
def test_fill_rejects_number_granularity(sparse_orders) -> None:
    from tests.models import Order

    with pytest.raises(AggregateError, match="TimeGranularity"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("created_at", NumberGranularity.MONTH_NUMBER)],
            aggregates=[(AggregateOp.COUNT, None)],
            fill=True,
        )


@pytest.mark.django_db
def test_fill_rejects_non_granular_group_by(sparse_orders) -> None:
    from tests.models import Order

    with pytest.raises(AggregateError, match="TimeGranularity"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("status", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            fill=True,
        )


@pytest.mark.django_db
def test_fill_min_max_without_fill_raises(sparse_orders) -> None:
    """``fill_min`` / ``fill_max`` are meaningless without ``fill=True``
    and must raise rather than silently no-op."""
    from tests.models import Order

    with pytest.raises(AggregateError, match="fill_min"):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("created_at", TimeGranularity.MONTH)],
            aggregates=[(AggregateOp.COUNT, None)],
            fill_min=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        )


@pytest.mark.django_db
def test_fill_empty_dataset_with_explicit_bounds(db) -> None:
    """No rows at all + explicit bounds → spine of zero-count rows."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.MONTH)],
        aggregates=[(AggregateOp.COUNT, None)],
        fill=True,
        fill_min=datetime.datetime(2026, 1, 1, tzinfo=UTC),
        fill_max=datetime.datetime(2026, 3, 1, tzinfo=UTC),
    )
    assert len(rows) == 3
    assert all(r["count"] == 0 for r in rows)


@pytest.mark.django_db
def test_fill_week_grain_with_week_start_sunday(db) -> None:
    """WEEK granularity + ``week_start=7`` — fill spine respects the
    user-supplied first day of week."""
    from tests.models import Customer, Order

    c = Customer.objects.create(name="WeekFill")
    # Two orders three weeks apart — Apr 26 (Sun) and May 17 (Sun).
    Order.objects.create(
        customer=c, status="paid", total=Decimal("100.00"), quantity=1,
        created_at=datetime.datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
    )
    Order.objects.create(
        customer=c, status="paid", total=Decimal("100.00"), quantity=1,
        created_at=datetime.datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
    )

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("created_at", TimeGranularity.WEEK)],
        aggregates=[(AggregateOp.COUNT, None)],
        fill=True,
        week_start=7,  # Sunday-first
    )
    # Spine: Sun Apr 26, Sun May 3, Sun May 10, Sun May 17.
    assert len(rows) == 4
    weeks = [r["created_at_week"].date() for r in rows]
    assert weeks == [
        datetime.date(2026, 4, 26),
        datetime.date(2026, 5, 3),
        datetime.date(2026, 5, 10),
        datetime.date(2026, 5, 17),
    ]
    counts = [r["count"] for r in rows]
    assert counts == [1, 0, 0, 1]


# ---------------------------------------------------------------------------
# 4 · GraphQL surface — fill / fillMin / fillMax args
# ---------------------------------------------------------------------------


@pytest.fixture
def schema_with_dates(sparse_orders):
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer", "status", "created_at"],
    ).build()

    @strawberry.type
    class Query:
        order_aggregate:  built.aggregate_type     = built.aggregate_field
        orders_group_by:  built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query), built


@pytest.mark.django_db
def test_graphql_fill_default_off(schema_with_dates) -> None:
    """``fill`` defaults to False — sparse months stay sparse."""
    schema, _ = schema_with_dates
    result = schema.execute_sync("""
        query {
          ordersGroupBy(groupBy: [
            { field: CREATED_AT, granularity: MONTH }
          ]) {
            results { key { createdAtMonth } count }
            totalCount
          }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    assert len(rows) == 3  # Jan, Mar, Jun
    assert result.data["ordersGroupBy"]["totalCount"] == 3


@pytest.mark.django_db
def test_graphql_fill_true_inserts_empty_buckets(schema_with_dates) -> None:
    schema, _ = schema_with_dates
    result = schema.execute_sync("""
        query {
          ordersGroupBy(
            groupBy: [{ field: CREATED_AT, granularity: MONTH }]
            fill: true
          ) {
            results { key { createdAtMonth } count }
            totalCount
          }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    assert len(rows) == 6  # Jan..Jun
    counts = [r["count"] for r in rows]
    assert counts == [1, 0, 1, 0, 0, 1]
    assert result.data["ordersGroupBy"]["totalCount"] == 6


@pytest.mark.django_db
def test_graphql_fill_min_max_extend_window(schema_with_dates) -> None:
    schema, _ = schema_with_dates
    result = schema.execute_sync("""
        query {
          ordersGroupBy(
            groupBy: [{ field: CREATED_AT, granularity: MONTH }]
            fill: true
            fillMin: "2025-12-01T00:00:00+00:00"
            fillMax: "2026-08-01T00:00:00+00:00"
          ) {
            results { key { createdAtMonth } count }
            totalCount
          }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    assert len(rows) == 9  # Dec 2025..Aug 2026
    assert result.data["ordersGroupBy"]["totalCount"] == 9


@pytest.mark.django_db
def test_graphql_fill_with_invalid_group_by_raises(schema_with_dates) -> None:
    schema, _ = schema_with_dates
    result = schema.execute_sync("""
        query {
          ordersGroupBy(
            groupBy: [{ field: STATUS }]
            fill: true
          ) {
            results { key { status } count }
          }
        }
    """)
    assert result.errors is not None
    # The error should mention the TIME-granularity restriction.
    assert any("TimeGranularity" in str(e) or "TIME-granularity" in str(e)
               for e in result.errors)


# ---------------------------------------------------------------------------
# 5 · SDL determinism — fill args appear unconditionally
# ---------------------------------------------------------------------------


def test_sdl_contains_fill_args(db) -> None:
    """The grouped field exposes ``fill``/``fillMin``/``fillMax`` regardless
    of any flag. Rule 2 — emission stays stable."""
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["customer", "created_at"],
    ).build()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    sdl = strawberry.Schema(query=Query).as_str()
    assert "fill" in sdl
    assert "fillMin" in sdl
    assert "fillMax" in sdl


def test_sdl_byte_identical_with_fill_args(db) -> None:
    """Build the schema twice — the new args must not introduce
    non-determinism."""
    from tests.models import Order

    def _build_sdl() -> str:
        built = AggregateBuilder(
            model=Order,
            aggregate_fields=["total", "quantity"],
            group_by_fields=["customer", "status", "created_at"],
        ).build()

        @strawberry.type
        class Query:
            order_aggregate:  built.aggregate_type     = built.aggregate_field
            orders_group_by:  built.grouped_result_type = built.group_by_field

        return strawberry.Schema(query=Query).as_str()

    sdl_1 = _build_sdl()
    sdl_2 = _build_sdl()
    assert sdl_1 == sdl_2


# ---------------------------------------------------------------------------
# 6 · Public-API smoke — exports
# ---------------------------------------------------------------------------


def test_fill_helpers_are_exported() -> None:
    import strawberry_django_aggregates as sda

    assert sda.fill_bucket_results is fill_bucket_results
    assert sda.generate_bucket_spine is generate_bucket_spine
    assert "fill_bucket_results" in sda.__all__
    assert "generate_bucket_spine" in sda.__all__
