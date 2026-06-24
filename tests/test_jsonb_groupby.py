"""JSON-path group_by and aggregation — Stream 17 / SPEC § 6.1.

Validates:

- Group by ``metadata.region`` on a ``JSONField`` column produces
  string-bucketed rows.
- ``SUM`` over ``metadata.amount`` declared as ``Decimal`` applies the
  ``Cast`` wrap so non-string values aggregate correctly.
- Date-typed JSON paths (``metadata.created_at_iso`` / ``"datetime"``)
  accept ``TimeGranularity`` for bucketed group_by.
- HAVING on ``sum_metadata__amount > 100`` filters as expected.
- Paths missing from the ``json_paths`` allowlist raise
  :class:`JSONPathNotAllowed` at resolver entry.
- Determinism: SDL emission with ``json_paths`` is byte-identical
  across two builds (CLAUDE.md Critical Rule 2).
"""

import datetime
from decimal import Decimal

import pytest
import strawberry

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateOp,
    JSONPathNotAllowed,
    TimeGranularity,
    compute_aggregation,
)

# Skip the entire JSONField test suite on database backends without
# native JSONField support (every supported vendor has it as of Django
# 4.0; this is a defensive check for unusual backends).
pytestmark = pytest.mark.django_db


@pytest.fixture
def jsonb_orders(db):
    """Orders with JSONB metadata. Layout (region, amount, created_at):

    - r1 (north, 100, 2026-04-01)
    - r2 (north, 200, 2026-04-15)
    - r3 (south, 300, 2026-05-05)
    - r4 (south,  50, 2026-05-10)
    - r5 (east,   75, 2026-05-20)
    - r6 (north, 400, 2026-05-25)

    Sums per region: north=700, south=350, east=75. Sums per month:
    April=300 (north), May=825 (north 400 + south 350 + east 75).
    """
    from tests.models import Customer, Order

    c = Customer.objects.create(name="JSONB-Co")
    tz = datetime.UTC
    rows = [
        ("north", "100", datetime.datetime(2026, 4, 1, 12, tzinfo=tz)),
        ("north", "200", datetime.datetime(2026, 4, 15, 9, tzinfo=tz)),
        ("south", "300", datetime.datetime(2026, 5, 5, 14, tzinfo=tz)),
        ("south",  "50", datetime.datetime(2026, 5, 10, 23, tzinfo=tz)),
        ("east",   "75", datetime.datetime(2026, 5, 20, 1, tzinfo=tz)),
        ("north", "400", datetime.datetime(2026, 5, 25, 16, tzinfo=tz)),
    ]
    created: list[Order] = []
    for region, amount, ts in rows:
        created.append(Order.objects.create(
            customer=c,
            status="paid",
            total=Decimal(amount),
            quantity=1,
            is_priority=False,
            created_at=ts,
            metadata={
                "region": region,
                "amount": amount,
                "created_at_iso": ts.isoformat(),
            },
        ))
    return created


def test_group_by_metadata_region_strings(jsonb_orders):
    """Group by ``metadata.region`` on JSONB returns three buckets."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("metadata.region", None)],
        aggregates=[(AggregateOp.COUNT, None)],
        json_paths={"metadata.region": "str"},
        order_by=[("metadata__region", "asc", None)],
    )
    by_region = {r["metadata__region"]: r["count"] for r in rows}
    assert by_region == {"north": 3, "south": 2, "east": 1}


def test_sum_metadata_amount_decimal_cast(jsonb_orders):
    """SUM over ``metadata.amount`` declared as Decimal applies Cast.

    Without the Cast wrap the JSONB text value would not aggregate
    numerically (would either error or sum lexicographically). The
    declared-Decimal type is the contract that enforces the right
    output_field on the inner ``KeyTextTransform``.
    """
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        aggregates=[
            (AggregateOp.SUM, "metadata.amount"),
            (AggregateOp.MIN, "metadata.amount"),
            (AggregateOp.MAX, "metadata.amount"),
        ],
        json_paths={"metadata.amount": "Decimal"},
    )
    row = rows[0]
    assert row["sum_metadata__amount"] == Decimal("1125")
    assert row["min_metadata__amount"] == Decimal("50")
    assert row["max_metadata__amount"] == Decimal("400")


def test_group_by_metadata_region_with_decimal_sum(jsonb_orders):
    """Group by JSON path AND aggregate a different JSON path."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("metadata.region", None)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "metadata.amount"),
        ],
        json_paths={
            "metadata.region": "str",
            "metadata.amount": "Decimal",
        },
        order_by=[("metadata__region", "asc", None)],
    )
    by_region = {r["metadata__region"]: r for r in rows}
    assert by_region["north"]["count"] == 3
    assert by_region["north"]["sum_metadata__amount"] == Decimal("700")
    assert by_region["south"]["count"] == 2
    assert by_region["south"]["sum_metadata__amount"] == Decimal("350")
    assert by_region["east"]["count"] == 1
    assert by_region["east"]["sum_metadata__amount"] == Decimal("75")


def test_group_by_metadata_datetime_with_month_granularity(jsonb_orders):
    """Date-typed JSON path accepts ``TimeGranularity.MONTH``.

    Validates the basic month-bucketed group_by on a JSONB datetime
    column. SPEC § 6.1 documents tz-correct bucketing as best-effort
    on SQLite; the basic UTC bucketing works there in practice via
    Django's ``Cast(KeyTextTransform, DateTimeField)`` + ``Trunc``
    pipeline.
    """
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[(
            "metadata.created_at_iso", TimeGranularity.MONTH,
        )],
        aggregates=[(AggregateOp.COUNT, None)],
        json_paths={"metadata.created_at_iso": "datetime"},
        order_by=[("metadata__created_at_iso_month", "asc", None)],
    )
    counts = [r["count"] for r in rows]
    assert counts == [2, 4]


def test_having_on_json_sum(jsonb_orders):
    """HAVING with ``sum_metadata__amount__gt`` filters group buckets."""
    from tests.models import Order

    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("metadata.region", None)],
        aggregates=[
            (AggregateOp.COUNT, None),
            (AggregateOp.SUM, "metadata.amount"),
        ],
        having={"sum_metadata__amount__gt": Decimal("100")},
        json_paths={
            "metadata.region": "str",
            "metadata.amount": "Decimal",
        },
        order_by=[("metadata__region", "asc", None)],
    )
    # north (700) and south (350) pass, east (75) is filtered out.
    regions = sorted(r["metadata__region"] for r in rows)
    assert regions == ["north", "south"]


def test_unallowlisted_json_path_raises(jsonb_orders):
    """A dotted path on a JSONField NOT in ``json_paths`` raises."""
    from tests.models import Order

    with pytest.raises(JSONPathNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("metadata.unauthorized_key", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            json_paths={"metadata.region": "str"},
        )


def test_unallowlisted_json_path_with_no_allowlist_raises(jsonb_orders):
    """Passing no ``json_paths`` at all still raises on JSONB paths."""
    from tests.models import Order

    with pytest.raises(JSONPathNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            aggregates=[(AggregateOp.SUM, "metadata.amount")],
        )


def test_nested_dotted_path_rejected(jsonb_orders):
    """Multi-level nesting is out of scope for v1.0."""
    from tests.models import Order

    with pytest.raises(JSONPathNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("metadata.address.city", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            json_paths={"metadata.address.city": "str"},
        )


def test_dotted_path_on_non_json_field_falls_through(jsonb_orders):
    """A dotted path whose first segment is NOT a JSONField falls
    through to the regular field-resolution path — which raises
    because the field doesn't exist (single-segment validation).
    """
    from strawberry_django_aggregates.errors import (
        AggregateError,
        GroupByFieldNotAllowed,
    )
    from tests.models import Order

    # `total.foo` — `total` is a DecimalField, not a JSONField. The
    # JSON-path branch returns None and the regular resolver raises.
    with pytest.raises((GroupByFieldNotAllowed, AggregateError)):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("total.foo", None)],
            aggregates=[(AggregateOp.COUNT, None)],
            json_paths={"total.foo": "str"},
        )


# ---------------------------------------------------------------------------
# Determinism — JSON paths must produce byte-identical SDL across runs.
# ---------------------------------------------------------------------------


def _build_jsonb_schema():
    """Build a schema with json_paths declared and return its SDL."""
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "metadata.amount"],
        group_by_fields=["customer", "metadata.region"],
        json_paths={
            "metadata.region": "str",
            "metadata.amount": "Decimal",
        },
    ).build()

    @strawberry.type
    class Query:
        order_aggregate:  built.aggregate_type     = built.aggregate_field
        orders_group_by:  built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query).as_str()


def test_jsonb_schema_byte_identical_across_runs(db):
    sdl_a = _build_jsonb_schema()
    sdl_b = _build_jsonb_schema()
    assert sdl_a == sdl_b


def test_jsonb_schema_independent_of_json_paths_dict_order(db):
    """Iteration order of ``json_paths`` must not leak into SDL.

    Uses the same Query class name for both schemas so the
    top-level ``schema { query: Query }`` line is identical and
    the diff is constrained to JSON-path-derived shape.
    """

    def build(json_paths):
        from tests.models import Order

        b = AggregateBuilder(
            model=Order,
            aggregate_fields=["metadata.amount"],
            group_by_fields=["metadata.region"],
            json_paths=json_paths,
        ).build()

        @strawberry.type
        class Query:
            a: b.aggregate_type     = b.aggregate_field
            g: b.grouped_result_type = b.group_by_field

        return strawberry.Schema(query=Query).as_str()

    sdl_a = build({
        "metadata.region": "str",
        "metadata.amount": "Decimal",
    })
    sdl_b = build({
        "metadata.amount": "Decimal",
        "metadata.region": "str",
    })
    assert sdl_a == sdl_b


# ---------------------------------------------------------------------------
# End-to-end GraphQL — JSON paths through the resolver path.
# ---------------------------------------------------------------------------


def test_graphql_group_by_json_region(jsonb_orders):
    """End-to-end: groupBy on metadata.region via the GraphQL resolver."""
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["metadata.amount"],
        group_by_fields=["metadata.region"],
        json_paths={
            "metadata.region": "str",
            "metadata.amount": "Decimal",
        },
    ).build()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    schema = strawberry.Schema(query=Query)
    # The wire form uses the enum value ``METADATA__REGION`` (alias of
    # the dotted ``metadata.region``). Strawberry converts the Python
    # identifier ``metadata__region`` to GraphQL ``metadata_Region``
    # (the standard camelCase rule keeps the single underscore +
    # capital). Both forms are valid GraphQL identifiers; the dotted
    # form would not be.
    result = schema.execute_sync(
        """
        query {
          ordersGroupBy(
            groupBy: [{ field: METADATA__REGION }]
          ) {
            results {
              key { metadata_Region }
              count
              sum { metadata_Amount }
            }
          }
        }
        """,
    )
    assert result.errors is None, result.errors
    rows = {
        r["key"]["metadata_Region"]: r
        for r in result.data["ordersGroupBy"]["results"]
    }
    assert rows["north"]["count"] == 3
    assert rows["north"]["sum"]["metadata_Amount"] == "700"
    assert rows["south"]["count"] == 2
    assert rows["south"]["sum"]["metadata_Amount"] == "350"
    assert rows["east"]["count"] == 1
    assert rows["east"]["sum"]["metadata_Amount"] == "75"


def test_graphql_group_by_direct_json_field_returns_json_key(db):
    """A direct JSONField group key keeps JSON, not String.

    Direct JSONField grouping differs from ``metadata.region`` JSON-path
    grouping: the bucket value can be an object or array. The GraphQL key
    field must therefore use the JSON scalar so list values serialize.
    """
    from tests.models import Customer, Order

    customer = Customer.objects.create(name="JSON-list-Co")
    stamp = datetime.datetime(2026, 6, 1, 12, tzinfo=datetime.UTC)
    payloads = (
        ["strategy", "q3"],
        ["strategy", "q3"],
        ["support", "triage"],
    )
    for payload in payloads:
        Order.objects.create(
            customer=customer,
            status="paid",
            total=Decimal("10"),
            quantity=1,
            is_priority=False,
            created_at=stamp,
            metadata=payload,
        )

    def _build():
        built = AggregateBuilder(
            model=Order,
            aggregate_fields=[],
            group_by_fields=["metadata"],
        ).build()

        @strawberry.type
        class Query:
            orders_group_by: built.grouped_result_type = (
                built.group_by_field
            )

        return strawberry.Schema(query=Query)

    schema = _build()
    sdl = schema.as_str()
    assert "metadata: JSON" in sdl, sdl
    assert "metadata: String" not in sdl, sdl
    # Determinism (Critical Rule 2): a second independent build of the
    # direct-JSONField group_by emits byte-identical SDL.
    assert _build().as_str() == sdl

    result = schema.execute_sync(
        """
        query {
          ordersGroupBy(groupBy: [{ field: METADATA }]) {
            results {
              key { metadata }
              count
            }
          }
        }
        """,
    )

    assert result.errors is None, result.errors
    rows = {
        tuple(row["key"]["metadata"]): row["count"]
        for row in result.data["ordersGroupBy"]["results"]
    }
    assert rows == {
        ("strategy", "q3"): 2,
        ("support", "triage"): 1,
    }


def test_direct_json_measure_override_emits_json(db):
    """An explicitly allowlisted JSONField measure surfaces as JSON.

    ``JSONField`` is absent from ``default_operators_for`` (never a
    measure by default), but a caller may allowlist MIN/MAX/ARRAY_AGG
    on the bare column via ``operators``. The natural output type of
    those ops over a JSON column is the JSON scalar, not String —
    locking the SPEC § 6.1 promise for the override path.
    """
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["metadata"],
        group_by_fields=["status"],
        operators={"metadata": (AggregateOp.MIN, AggregateOp.MAX)},
    ).build()

    @strawberry.type
    class Query:
        order_aggregate: built.aggregate_type = built.aggregate_field

    sdl = strawberry.Schema(query=Query).as_str()
    assert "type OrderMinFields {\n  metadata: JSON\n}" in sdl, sdl
    assert "type OrderMaxFields {\n  metadata: JSON\n}" in sdl, sdl
