"""Grouped filter echo — SPEC § 4.4.

Each grouped bucket can expose ``filter: JSON!`` (opt-in via
``enable_filter_echo``) whose value is shaped like the list query's
``filter:`` argument and re-selects that bucket's rows. Exercises the
full stack: type emission → resolver → bucket→filter translation.

No ``from __future__ import annotations`` — strawberry resolves the
Query field annotations against the live ``built.*`` class objects.
"""

import datetime
from decimal import Decimal

import pytest
import strawberry
import strawberry_django
from django.db import models
from strawberry_django.fields.filter_types import FilterLookup

from strawberry_django_aggregates import AggregateBuilder, FilterEchoError
from tests.models import Order

pytestmark = pytest.mark.django_db


@strawberry_django.filter_type(Order, lookups=True)
class OrderFilter:
    status:      strawberry.auto
    total:       strawberry.auto
    quantity:    strawberry.auto
    is_priority: strawberry.auto
    created_at:  strawberry.auto
    customer:    strawberry.auto


@strawberry_django.filter_type(Order, lookups=True)
class OrderFilterNoCustomer:
    """Filter type WITHOUT ``customer`` — exercises the fail-loud
    "filter type has no such field" path for an FK axis.
    """
    status:     strawberry.auto
    created_at: strawberry.auto


@strawberry.enum
class StatusEnum(models.TextChoices):
    # A choices vocabulary exposed as a GraphQL enum input — wire names
    # (DRAFT) differ from the stored values (draft). TextChoices members
    # are str-comparable so Django filtering matches stored rows.
    DRAFT = "draft", "Draft"
    PAID = "paid", "Paid"
    CANCELLED = "cancelled", "Cancelled"


@strawberry_django.filter_type(Order, lookups=True)
class OrderEnumFilter:
    """`status` typed as a GraphQL **enum** lookup (e.g. a
    django-choices-field column) rather than a plain string lookup.
    """
    status:     FilterLookup[StatusEnum] | None
    created_at: strawberry.auto


@strawberry.enum
class StatusAliasEnum(models.TextChoices):
    # Member NAMES deliberately diverge from the aggregates-derived group
    # key names (PAID / DRAFT / CANCELLED) for the SAME stored values, so
    # a test can prove the echo emits the FILTER enum's wire name (keyed
    # on the stored value), not the group key's name.
    PENDING = "paid", "Paid"
    INITIAL = "draft", "Draft"
    VOID = "cancelled", "Cancelled"


@strawberry_django.filter_type(Order, lookups=True)
class OrderAliasFilter:
    status: FilterLookup[StatusAliasEnum] | None


@strawberry_django.type(Order)
class OrderRow:
    id: strawberry.auto


def _list_schema():
    """A plain strawberry-django list query filtered by ``OrderFilter`` —
    used to replay an echoed bucket filter and prove it re-selects
    exactly that bucket's rows.
    """
    @strawberry.type
    class Query:
        orders: list[OrderRow] = strawberry_django.field(
            filters=OrderFilter,
        )

    return strawberry.Schema(query=Query)


def _build(pagination_style="offset", filter_type=OrderFilter):
    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=[
            "customer", "status", "quantity", "is_priority",
            "created_at", "total",
        ],
        filter_type=filter_type,
        enable_filter_echo=True,
        pagination_style=pagination_style,
    ).build()
    return built


@pytest.fixture
def echo_schema():
    built = _build()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query)


@pytest.fixture
def echo_orders(db):
    """Three orders under one customer with a null-total row.

    quantity buckets: {2: 2, 5: 1}; total buckets: {100, 200, NULL};
    created_at months: {2026-04: 2, 2026-05: 1}; status: {paid: 2,
    draft: 1}.
    """
    from tests.models import Customer

    tz = datetime.UTC
    c = Customer.objects.create(name="Echo")
    Order.objects.create(
        customer=c, status="paid", total=Decimal("100.00"), quantity=2,
        is_priority=True,
        created_at=datetime.datetime(2026, 4, 1, 12, tzinfo=tz),
    )
    Order.objects.create(
        customer=c, status="paid", total=Decimal("200.00"), quantity=2,
        is_priority=True,
        created_at=datetime.datetime(2026, 4, 20, 12, tzinfo=tz),
    )
    Order.objects.create(
        customer=c, status="draft", total=None, quantity=5,
        is_priority=False,
        created_at=datetime.datetime(2026, 5, 5, 12, tzinfo=tz),
    )
    return c


# --------------------------------------------------------------------------
# Success paths
# --------------------------------------------------------------------------

def test_exact_scalar_bucket(echo_schema, echo_orders):
    """A plain scalar axis echoes ``{field: {exact: value}}``."""
    result = echo_schema.execute_sync("""
        query {
            ordersGroupBy(groupBy: [{ field: QUANTITY }]) {
                results { key { quantity } count filter }
            }
        }
    """)
    assert result.errors is None, result.errors
    by_qty = {
        r["key"]["quantity"]: r["filter"]
        for r in result.data["ordersGroupBy"]["results"]
    }
    assert by_qty[2] == {"quantity": {"exact": 2}}
    assert by_qty[5] == {"quantity": {"exact": 5}}


def test_boolean_exact_bucket(echo_schema, echo_orders):
    result = echo_schema.execute_sync("""
        query {
            ordersGroupBy(groupBy: [{ field: IS_PRIORITY }]) {
                results { key { isPriority } filter }
            }
        }
    """)
    assert result.errors is None, result.errors
    by_flag = {
        r["key"]["isPriority"]: r["filter"]
        for r in result.data["ordersGroupBy"]["results"]
    }
    assert by_flag[True] == {"isPriority": {"exact": True}}
    assert by_flag[False] == {"isPriority": {"exact": False}}


def test_null_bucket_echoes_is_null(echo_schema, echo_orders):
    """A NULL bucket key echoes ``{field: {isNull: true}}``."""
    result = echo_schema.execute_sync("""
        query {
            ordersGroupBy(groupBy: [{ field: TOTAL }]) {
                results { key { total } filter }
            }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    null_bucket = next(r for r in rows if r["key"]["total"] is None)
    assert null_bucket["filter"] == {"total": {"isNull": True}}
    # And a non-null decimal bucket round-trips its value as a string.
    hundred = next(r for r in rows if r["key"]["total"] == "100.00")
    assert hundred["filter"] == {"total": {"exact": "100.00"}}


def test_time_granularity_bucket_echoes_half_open_range(
    echo_schema, echo_orders,
):
    """A TIME-granularity bucket echoes a half-open ``{gte, lt}`` range
    (never the inclusive strawberry-django ``range`` lookup).
    """
    result = echo_schema.execute_sync("""
        query {
            ordersGroupBy(
                groupBy: [{ field: CREATED_AT, granularity: MONTH }]
            ) {
                results { key { createdAtMonth } filter }
            }
        }
    """)
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    # April bucket: [2026-04-01, 2026-05-01)
    april = next(
        r for r in rows
        if r["key"]["createdAtMonth"].startswith("2026-04")
    )
    created = april["filter"]["createdAt"]
    assert set(created) == {"gte", "lt"}
    assert created["gte"].startswith("2026-04-01")
    assert created["lt"].startswith("2026-05-01")


def test_multi_axis_implicit_and(echo_schema, echo_orders):
    """Distinct axes combine as implicit AND across filter fields."""
    result = echo_schema.execute_sync("""
        query {
            ordersGroupBy(groupBy: [
                { field: QUANTITY },
                { field: CREATED_AT, granularity: MONTH }
            ]) {
                results { key { quantity createdAtMonth } filter }
            }
        }
    """)
    assert result.errors is None, result.errors
    bucket = next(
        r for r in result.data["ordersGroupBy"]["results"]
        if r["key"]["quantity"] == 2
        and r["key"]["createdAtMonth"].startswith("2026-04")
    )
    assert bucket["filter"]["quantity"] == {"exact": 2}
    assert set(bucket["filter"]["createdAt"]) == {"gte", "lt"}


def test_foreign_key_axis_echoes_pk(echo_schema, echo_orders):
    """An FK axis echoes ``{field: {pk: <id>}}`` — strawberry-django's
    relation-filter shape — not ``{exact}``.
    """
    result = echo_schema.execute_sync("""
        query {
            ordersGroupBy(groupBy: [{ field: CUSTOMER }]) {
                results { key { customerId } filter }
            }
        }
    """)
    assert result.errors is None, result.errors
    bucket = result.data["ordersGroupBy"]["results"][0]
    # Shape: a relation `pk` lookup, NOT `exact`. The pk value is the
    # group key's id (compared type-insensitively — the echo emits the
    # raw id, the key serializes it through the GraphQL ID scalar; both
    # name the same row, and the round-trip test proves it re-selects).
    assert set(bucket["filter"]) == {"customer"}
    assert set(bucket["filter"]["customer"]) == {"pk"}
    assert str(bucket["filter"]["customer"]["pk"]) == str(
        bucket["key"]["customerId"],
    )


def test_repeated_axis_nests_and_without_dropping(echo_schema, echo_orders):
    """Two granularities on the same date axis both constrain
    ``createdAt`` — the extra clause folds into a nested ``AND`` rather
    than being silently dropped (Rule 6).
    """
    result = echo_schema.execute_sync("""
        query {
            ordersGroupBy(groupBy: [
                { field: CREATED_AT, granularity: MONTH },
                { field: CREATED_AT, granularity: DAY }
            ]) {
                results { filter }
            }
        }
    """)
    assert result.errors is None, result.errors
    f = result.data["ordersGroupBy"]["results"][0]["filter"]
    # One createdAt clause at top level, the other under a nested AND.
    assert "createdAt" in f
    assert "AND" in f and "createdAt" in f["AND"]
    assert f["createdAt"] != f["AND"]["createdAt"]


@pytest.mark.parametrize("group_by", [
    "[{ field: STATUS }]",
    "[{ field: CUSTOMER }]",
    "[{ field: QUANTITY }]",
    "[{ field: TOTAL }]",
    "[{ field: CREATED_AT, granularity: MONTH }]",
    "[{ field: STATUS }, { field: CUSTOMER }]",
])
def test_echo_roundtrip_reselects_bucket_rows(
    echo_schema, echo_orders, group_by,
):
    """The load-bearing guarantee: replaying a bucket's echoed ``filter``
    through the actual list query returns exactly that bucket's rows.

    This is what catches enum-name-vs-stored-value and FK-shape bugs that
    a shape-only assertion misses.
    """
    grouped = echo_schema.execute_sync(
        "query { ordersGroupBy(groupBy: "
        + group_by
        + ") { results { count filter } } }",
    )
    assert grouped.errors is None, grouped.errors
    buckets = grouped.data["ordersGroupBy"]["results"]
    assert buckets  # the fixture has rows

    list_schema = _list_schema()
    for r in buckets:
        replayed = list_schema.execute_sync(
            "query($f: OrderFilter!) { orders(filters: $f) { id } }",
            variable_values={"f": r["filter"]},
        )
        assert replayed.errors is None, (group_by, r["filter"],
                                         replayed.errors)
        assert len(replayed.data["orders"]) == r["count"], (
            f"echoed filter {r['filter']} re-selected "
            f"{len(replayed.data['orders'])} rows, expected {r['count']}"
        )


def test_enum_typed_lookup_echoes_wire_name(echo_orders):
    """When the list filter types a choices column as a GraphQL **enum**
    input, the echo emits the enum WIRE NAME (`DRAFT`), not the stored
    value (`draft`) — otherwise it fails enum-input validation and
    re-selects nothing. The string-lookup round-trip above proves the
    opposite case still emits the stored value.
    """
    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["status", "created_at"],
        filter_type=OrderEnumFilter,
        enable_filter_echo=True,
    ).build()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    schema = strawberry.Schema(query=Query)
    grouped = schema.execute_sync("""
        query {
            ordersGroupBy(groupBy: [{ field: STATUS }]) {
                results { key { status } count filter }
            }
        }
    """)
    assert grouped.errors is None, grouped.errors
    rows = grouped.data["ordersGroupBy"]["results"]
    assert rows
    for r in rows:
        # Wire name (e.g. PAID), matching the enum-serialized group key —
        # NOT the stored value (paid).
        echoed = r["filter"]["status"]["exact"]
        assert echoed == r["key"]["status"]
        assert echoed.isupper()

    # Round-trip through an enum-typed list filter: the echoed name must
    # be a valid enum input AND re-select exactly the bucket's rows.
    @strawberry.type
    class ListQuery:
        orders: list[OrderRow] = strawberry_django.field(
            filters=OrderEnumFilter,
        )

    list_schema = strawberry.Schema(query=ListQuery)
    for r in rows:
        replayed = list_schema.execute_sync(
            "query($f: OrderEnumFilter!) { orders(filters: $f) { id } }",
            variable_values={"f": r["filter"]},
        )
        assert replayed.errors is None, (r["filter"], replayed.errors)
        assert len(replayed.data["orders"]) == r["count"]


def test_enum_lookup_uses_filter_enum_name_not_key_name(echo_orders):
    """The echoed name comes from the FILTER enum (keyed on the bucket's
    stored value), not the group key's enum. Here the two enums assign
    DIFFERENT names to the same stored value (`paid` → key `PAID`, filter
    `PENDING`), so mirroring the key would emit the wrong — and
    invalid — name. Guards against a regression to key-mirroring.
    """
    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["status"],
        filter_type=OrderAliasFilter,
        enable_filter_echo=True,
    ).build()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    schema = strawberry.Schema(query=Query)
    grouped = schema.execute_sync("""
        query {
            ordersGroupBy(groupBy: [{ field: STATUS }]) {
                results { key { status } count filter }
            }
        }
    """)
    assert grouped.errors is None, grouped.errors
    rows = grouped.data["ordersGroupBy"]["results"]
    assert rows
    stored_to_wire = {"paid": "PENDING", "draft": "INITIAL",
                      "cancelled": "VOID"}
    for r in rows:
        echoed = r["filter"]["status"]["exact"]
        # The echoed name is the FILTER enum's, which differs from the
        # group key's serialized name for the same stored value.
        assert echoed != r["key"]["status"]
        assert echoed in set(stored_to_wire.values())

    @strawberry.type
    class ListQuery:
        orders: list[OrderRow] = strawberry_django.field(
            filters=OrderAliasFilter,
        )

    list_schema = strawberry.Schema(query=ListQuery)
    for r in rows:
        replayed = list_schema.execute_sync(
            "query($f: OrderAliasFilter!) { orders(filters: $f) { id } }",
            variable_values={"f": r["filter"]},
        )
        assert replayed.errors is None, (r["filter"], replayed.errors)
        assert len(replayed.data["orders"]) == r["count"]


# --------------------------------------------------------------------------
# Laziness — no introspection unless the client selects ``filter``
# --------------------------------------------------------------------------

def test_filter_not_computed_when_unselected(echo_schema, echo_orders):
    """A bucket that would REFUSE to echo (NUMBER granularity) is fine as
    long as ``filter`` is not selected — the echo is lazy.
    """
    result = echo_schema.execute_sync("""
        query {
            ordersGroupBy(
                groupBy: [{ field: CREATED_AT, granularity: DAY_OF_WEEK }]
            ) {
                results { count }
            }
        }
    """)
    assert result.errors is None, result.errors
    assert sum(
        r["count"] for r in result.data["ordersGroupBy"]["results"]
    ) == 3


# --------------------------------------------------------------------------
# Fail-loud refusals (SPEC § 4.4)
# --------------------------------------------------------------------------

def test_number_granularity_refused_when_selected(echo_schema, echo_orders):
    result = echo_schema.execute_sync("""
        query {
            ordersGroupBy(
                groupBy: [{ field: CREATED_AT, granularity: DAY_OF_WEEK }]
            ) {
                results { count filter }
            }
        }
    """)
    assert result.errors is not None
    assert any(
        "NUMBER granularity" in str(e.message) for e in result.errors
    ), [str(e.message) for e in result.errors]


def test_missing_filter_field_refused(echo_orders):
    """Grouping by an axis absent from the filter type and selecting
    ``filter`` fails loud naming the field.
    """
    built = _build(filter_type=OrderFilterNoCustomer)

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    schema = strawberry.Schema(query=Query)
    result = schema.execute_sync("""
        query {
            ordersGroupBy(groupBy: [{ field: CUSTOMER }]) {
                results { key { customerId } filter }
            }
        }
    """)
    assert result.errors is not None
    assert any(
        "customer" in str(e.message) and "has no" in str(e.message)
        for e in result.errors
    ), [str(e.message) for e in result.errors]


def test_missing_lookup_refused_unit():
    """A field present on the filter type but whose lookup type lacks the
    needed lookup fails loud — the DRY guarantee that lookup names are
    resolved against the live type, not hardcoded.
    """
    built_builder = AggregateBuilder(
        model=Order,
        group_by_fields=["status"],
        filter_type=OrderFilter,
        enable_filter_echo=True,
    )
    # ``status`` is a string lookup (exact/is_null) — it has no ``gte``.
    with pytest.raises(FilterEchoError, match="lacks lookup"):
        built_builder._echo_field_filter("status", {"gte": "paid"})


def test_json_path_axis_refused_unit():
    """JSON-path group axes have no matching list-filter field."""
    builder = AggregateBuilder(
        model=Order,
        group_by_fields=["status"],
        filter_type=OrderFilter,
        json_paths={"metadata.region": "str"},
        enable_filter_echo=True,
    )
    with pytest.raises(FilterEchoError, match="JSON-path"):
        builder._echo_axis_filter(
            "metadata.region", None, {"metadata__region": "north"},
        )


def test_enable_filter_echo_without_filter_type_raises():
    with pytest.raises(FilterEchoError, match="filter_type"):
        AggregateBuilder(
            model=Order,
            group_by_fields=["status"],
            enable_filter_echo=True,
        ).build()


# --------------------------------------------------------------------------
# Symmetry across pagination styles
# --------------------------------------------------------------------------

def test_filter_echo_on_cursor_connection(echo_orders):
    built = _build(pagination_style="both")

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field
        orders_group_by_connection: built.grouped_connection_type = (
            built.grouped_connection_field
        )

    schema = strawberry.Schema(query=Query)
    result = schema.execute_sync("""
        query {
            ordersGroupByConnection(groupBy: [{ field: QUANTITY }]) {
                edges { node { key { quantity } filter } }
            }
        }
    """)
    assert result.errors is None, result.errors
    by_qty = {
        e["node"]["key"]["quantity"]: e["node"]["filter"]
        for e in result.data["ordersGroupByConnection"]["edges"]
    }
    assert by_qty[2] == {"quantity": {"exact": 2}}
    assert by_qty[5] == {"quantity": {"exact": 5}}
