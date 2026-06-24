"""Public ``AggregateBuilder.shape_group_key`` — parity with the private
grouped-shaping path, and composition with the free aggregate.

A consumer that builds its own grouped envelope (e.g. the Hasura/NDC
``<res>_group { key, aggregate }`` shape) pairs ``shape_group_key`` (the
typed ``<Model>GroupKey``) with the free ``<Model>Aggregate`` filled by
``shape_aggregate_row`` from the SAME row. These lock the public method to
the key the builder's own ``_shape_grouped`` produces across choices-enum,
FK ``_id``, and TIME ``BucketRange`` axes.
"""

from __future__ import annotations

import pytest

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateOp,
    BucketRange,
    TimeGranularity,
    compute_aggregation,
    shape_aggregate_row,
)


@pytest.mark.django_db
def test_shape_group_key_matches_private_shaping(sample_orders):
    """``shape_group_key`` returns a fresh key equal, by value, to the key
    the private ``_shape_grouped`` builds for the same row + spec —
    exercising FK (``customerId``), a choices column (``status`` → enum),
    and a TIME bucket (``createdAtMonth`` + ``createdAtMonthRange``)."""
    from tests.models import Order

    builder = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer", "status", "created_at"],
    )
    built = builder.build()

    spec = [
        ("customer", None),
        ("status", None),
        ("created_at", TimeGranularity.MONTH),
    ]
    requested = [(AggregateOp.COUNT, None), (AggregateOp.SUM, "total")]
    rows = compute_aggregation(
        Order.objects.all(), group_by=spec, aggregates=requested,
    )
    assert rows  # the fixture seeds orders across customers/months

    saw_paid_with_range = False
    for row in rows:
        public_key = builder.shape_group_key(
            built.group_key_type, row, spec,
        )
        private_key = builder._shape_grouped(
            built.grouped_type, built.group_key_type, row, requested, spec,
        ).key
        assert public_key is not private_key  # a fresh instance
        assert public_key == private_key  # equal by value

        # Positive value checks (not just parity-vs-private): the choices
        # axis is coerced to its enum member — never a raw str — and the
        # TIME axis carries a populated ``<alias>_range`` BucketRange.
        assert not isinstance(public_key.status, str)
        assert public_key.status.value == row["status"]
        assert isinstance(
            public_key.created_at_month_range, BucketRange,
        )
        if row["status"] == "paid":
            saw_paid_with_range = True
    assert saw_paid_with_range  # the fixture seeds paid orders


@pytest.mark.django_db
def test_shape_group_key_pairs_with_free_aggregate(sample_orders):
    """The adapter's intended composition: the typed key + the FREE
    ``<Model>Aggregate`` filled from the SAME row — no reshape."""
    from tests.models import Order

    builder = AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["status"],
    )
    built = builder.build()

    spec = [("status", None)]
    requested = [(AggregateOp.COUNT, None), (AggregateOp.SUM, "total")]
    rows = compute_aggregation(
        Order.objects.all(), group_by=spec, aggregates=requested,
    )
    assert rows
    row = rows[0]

    key = builder.shape_group_key(built.group_key_type, row, spec)
    aggregate = shape_aggregate_row(built.aggregate_type, row, requested)
    assert key is not None
    assert aggregate.count == int(row["count"])


@pytest.mark.django_db
def test_shape_group_key_json_path_axis_parity(sample_orders):
    """JSON-path group-by axis (SPEC § 6.1) — the branch the documented
    ``shape_aggregate_row(..., json_paths=builder.json_paths)`` asymmetry
    concerns. ``shape_group_key`` sources ``json_paths`` from the builder
    implicitly; assert it round-trips the ``metadata__region`` alias and
    stays in parity with ``_shape_grouped``, and that the paired free
    aggregate fills its JSON-path measure when given the same allowlist."""
    from tests.models import Order

    _customers, orders = sample_orders
    # ``sample_orders`` leaves ``metadata`` empty — seed a JSON region so
    # the axis produces real string buckets rather than a single None key.
    for i, order in enumerate(orders):
        order.metadata = {"region": "north" if i % 2 else "south"}
        order.save(update_fields=["metadata"])

    json_paths = {"metadata.region": "str"}
    builder = AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        # The dotted JSON path must be a declared group-by axis for the
        # emitted ``<Model>GroupKey`` to carry the ``metadata__region``
        # field — an explicit ``group_by_fields`` list does NOT auto-append
        # ``json_paths`` (types._resolve_group_by_fields).
        group_by_fields=["status", "metadata.region"],
        json_paths=json_paths,
    )
    built = builder.build()

    spec = [("metadata.region", None)]
    requested = [(AggregateOp.COUNT, None), (AggregateOp.SUM, "total")]
    rows = compute_aggregation(
        Order.objects.all(), group_by=spec, aggregates=requested,
        json_paths=json_paths,
    )
    assert {r["metadata__region"] for r in rows} == {"north", "south"}

    for row in rows:
        public_key = builder.shape_group_key(
            built.group_key_type, row, spec,
        )
        private_key = builder._shape_grouped(
            built.grouped_type, built.group_key_type, row, requested, spec,
        ).key
        assert public_key is not private_key
        assert public_key == private_key
        # The JSON-path alias round-trips onto the typed key.
        assert public_key.metadata__region == row["metadata__region"]

        # Documented composition: the free aggregate needs the SAME
        # allowlist passed explicitly to stay in parity with the key.
        aggregate = shape_aggregate_row(
            built.aggregate_type, row, requested,
            json_paths=builder.json_paths,
        )
        assert aggregate.count == int(row["count"])
