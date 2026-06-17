"""To-one relation traversal in group-by axes (forward FK / OneToOne).

A ``group_by_fields`` entry may walk a *forward to-one* relation
(ForeignKey / OneToOne) to a scalar leaf on the related model, e.g.
``customer__active`` on ``Order``. A to-one join matches at most one
related row per parent, so it cannot row-multiply — unlike o2m / m2m,
which remain refused as group-by axes (SPEC § 11). The group key field
is named with the full ``__`` path and typed from the leaf field.

Like ``test_choices_group_key.py`` this module omits ``from __future__
import annotations`` — under PEP 563 the dynamic ``built.*`` field-type
annotations on the ``Query`` class become strings Strawberry cannot
evaluate.
"""

import pytest
import strawberry
import strawberry_django

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateOp,
    FilterEchoError,
    compute_aggregation,
)
from strawberry_django_aggregates.errors import (
    AggregationAcrossRelationError,
    GroupByFieldNotAllowed,
)
from tests.models import Customer, Order, OrderItem


@strawberry_django.filter_type(Order, lookups=True)
class _EchoToOneFilter:
    """Minimal list filter used to construct an echo-enabled builder for
    the to-one refusal unit test. Never added to a schema — the refusal
    fires before any filter-type introspection.
    """
    customer: strawberry.auto
    status:   strawberry.auto


def _orders_by_customer_active_built():
    return AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["customer__active"],
    ).build()


def _orders_by_customer_active_sdl() -> str:
    built = _orders_by_customer_active_built()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query).as_str()


# ---------------------------------------------------------------------------
# backend primitive — compute_aggregation groups across the to-one join
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_compute_group_by_to_one_relation(sample_orders):
    """Group orders by their customer's ``active`` flag (FK → bool).

    From ``sample_orders``: Alpha (active=True) owns o1/o2/o6, Beta
    (active=True) owns o3/o4, Gamma (active=False) owns o5 — so
    ``active=True`` → 5 orders and ``active=False`` → 1.
    """
    rows = compute_aggregation(
        Order.objects.all(),
        group_by=[("customer__active", None)],
        aggregates=[(AggregateOp.COUNT, None)],
    )
    by_active = {r["customer__active"]: r["count"] for r in rows}
    assert by_active == {True: 5, False: 1}


# ---------------------------------------------------------------------------
# SDL emission — the key field and groupable-field enum carry the path
# ---------------------------------------------------------------------------

def test_group_key_emits_to_one_path_field(db):
    built = _orders_by_customer_active_built()
    # The dataclass field is named with the full Django path so the
    # grouped resolver's ``.values()`` alias round-trips onto the key.
    assert "customer__active" in built.group_key_type.__annotations__

    sdl = _orders_by_customer_active_sdl()
    # Strawberry camel-cases ``customer__active`` to ``customer_Active``;
    # the leaf bool keeps its scalar.
    assert "customer_Active: Boolean" in sdl, sdl
    # The groupable-field enum surfaces the alias-form member.
    assert "CUSTOMER__ACTIVE" in sdl, sdl


# ---------------------------------------------------------------------------
# GraphQL round-trip — request the axis and read the leaf value per bucket
# ---------------------------------------------------------------------------

def test_group_by_to_one_round_trip(sample_orders):
    built = _orders_by_customer_active_built()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    schema = strawberry.Schema(query=Query)
    result = schema.execute_sync(
        """
        query {
            ordersGroupBy(groupBy: [{ field: CUSTOMER__ACTIVE }]) {
                results {
                    key { customer_Active }
                    count
                }
            }
        }
        """,
    )
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    by_active = {r["key"]["customer_Active"]: r["count"] for r in rows}
    assert by_active == {True: 5, False: 1}


# ---------------------------------------------------------------------------
# to-many axes remain refused — building the type fails loud
# ---------------------------------------------------------------------------

def test_group_by_to_many_refused_at_build(db):
    """``orders__total`` walks Customer's reverse o2m — grouping across it
    would row-multiply, so the type emission refuses it (SPEC § 11).
    """
    with pytest.raises(AggregationAcrossRelationError):
        AggregateBuilder(
            model=Customer,
            aggregate_fields=["id"],
            group_by_fields=["orders__total"],
        ).build()


@pytest.mark.django_db
def test_compute_group_by_to_many_refused(sample_orders):
    """The compiler primitive refuses the same to-many axis."""
    with pytest.raises(AggregationAcrossRelationError):
        compute_aggregation(
            Customer.objects.all(),
            group_by=[("orders__total", None)],
            aggregates=[(AggregateOp.COUNT, None)],
        )


# ---------------------------------------------------------------------------
# determinism — byte-identical SDL across two builds
# ---------------------------------------------------------------------------

def test_to_one_group_by_sdl_is_deterministic(db):
    assert _orders_by_customer_active_sdl() == _orders_by_customer_active_sdl()


# ---------------------------------------------------------------------------
# cursor pagination over a to-one axis — regression: the cursor key
# extraction formerly called ``get_field`` directly and crashed on ``__``
# ---------------------------------------------------------------------------

def test_cursor_pagination_over_to_one_axis(sample_orders):
    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["customer__active"],
        pagination_style="cursor",
    ).build()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_connection_type = (
            built.grouped_connection_field
        )

    schema = strawberry.Schema(query=Query)
    result = schema.execute_sync(
        """
        query {
            ordersGroupBy(
                groupBy: [{ field: CUSTOMER__ACTIVE }], first: 10
            ) {
                edges { node { count key { customer_Active } } }
            }
        }
        """,
    )
    assert result.errors is None, result.errors
    edges = result.data["ordersGroupBy"]["edges"]
    by_active = {
        e["node"]["key"]["customer_Active"]: e["node"]["count"]
        for e in edges
    }
    assert by_active == {True: 5, False: 1}


# ---------------------------------------------------------------------------
# FK-leaf path — a to-one chain whose leaf is itself an FK surfaces as `_id`
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_fk_leaf_path_surfaces_id_alias(sample_order_items):
    """``OrderItem`` grouped by ``order__customer`` walks OrderItem → order
    (FK) → customer (FK leaf). The FK leaf surfaces as ``order__customer_id``
    on both the group key and the compiler row (SPEC § 6.2 / § 4).
    """
    customers, _orders, _items = sample_order_items
    a, b, g = customers

    built = AggregateBuilder(
        model=OrderItem,
        aggregate_fields=["price"],
        group_by_fields=["order__customer"],
    ).build()
    assert "order__customer_id" in built.group_key_type.__annotations__

    rows = compute_aggregation(
        OrderItem.objects.all(),
        group_by=[("order__customer", None)],
        aggregates=[(AggregateOp.COUNT, None)],
    )
    by_customer = {r["order__customer_id"]: r["count"] for r in rows}
    # Alpha owns o1(2)+o2(1)+o6(1)=4 items, Beta o3(2)=2, Gamma o5(3)=3.
    assert by_customer == {a.pk: 4, b.pk: 2, g.pk: 3}


# ---------------------------------------------------------------------------
# choices leaf reached through a to-one hop keeps its stored value in the row
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_choices_leaf_through_to_one(sample_order_items):
    """``OrderItem`` grouped by ``order__status`` reaches Order's choices
    column through the FK; the compiler row carries the stored value.
    """
    rows = compute_aggregation(
        OrderItem.objects.all(),
        group_by=[("order__status", None)],
        aggregates=[(AggregateOp.COUNT, None)],
    )
    by_status = {r["order__status"]: r["count"] for r in rows}
    # paid: o1(2)+o2(1)+o3(2)+o6(1)=6; draft: o5(3)=3;
    # cancelled (o4) has 0 items so no row.
    assert by_status == {"paid": 6, "draft": 3}


# ---------------------------------------------------------------------------
# multi-hop to-one chain — every segment forward-to-one is allowed
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_multi_hop_to_one_chain(sample_order_items):
    """Two-hop ``order__customer__active`` (OrderItem → order → customer →
    active) is allowed — no segment is to-many (SPEC § 6.2).
    """
    rows = compute_aggregation(
        OrderItem.objects.all(),
        group_by=[("order__customer__active", None)],
        aggregates=[(AggregateOp.COUNT, None)],
    )
    by_active = {r["order__customer__active"]: r["count"] for r in rows}
    # active (Alpha 4 + Beta 2 = 6); inactive (Gamma 3).
    assert by_active == {True: 6, False: 3}


# ---------------------------------------------------------------------------
# fail-loud on invalid / non-traversable paths (Critical Rule 6)
# ---------------------------------------------------------------------------

def test_invalid_to_one_leaf_fails_loud(db):
    """An unknown leaf on a valid to-one relation fails loud as
    ``GroupByFieldNotAllowed`` — never silently dropped.
    """
    with pytest.raises(GroupByFieldNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("customer__nonexistent", None)],
            aggregates=[(AggregateOp.COUNT, None)],
        )


def test_non_relation_middle_segment_fails_loud(db):
    """A ``__`` path whose middle segment is a scalar (``total`` is a
    ``DecimalField``, not a relation) cannot be traversed — fail loud.
    """
    with pytest.raises(GroupByFieldNotAllowed):
        compute_aggregation(
            Order.objects.all(),
            group_by=[("total__foo", None)],
            aggregates=[(AggregateOp.COUNT, None)],
        )


# ---------------------------------------------------------------------------
# filter echo refuses a to-one axis (SPEC § 6.2 / § 4.4)
# ---------------------------------------------------------------------------

def test_filter_echo_refuses_to_one_axis(db):
    """The related field is nested on the list filter, not flat, so echo
    refuses fail-loud rather than emit an unfaithful clause.
    """
    builder = AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["customer__active"],
        filter_type=_EchoToOneFilter,
        enable_filter_echo=True,
    )
    with pytest.raises(FilterEchoError, match="to-one"):
        builder._echo_axis_filter(
            "customer__active", None, {"customer__active": True},
        )


# ---------------------------------------------------------------------------
# choices-leaf enum is keyed on the full axis path, not the leaf field name —
# two axes sharing a leaf name with different choice sets must NOT collide.
# ---------------------------------------------------------------------------

def test_choices_enum_distinct_per_axis_path():
    """A direct ``status`` axis and a to-one ``shipment__status`` axis whose
    target has a same-named column with a *different* vocabulary must get
    distinct enums — the cache/type name keys on the full path (SPEC § 6.2).
    """
    from django.db import models as dj_models

    from strawberry_django_aggregates.types import _choices_enum_for

    direct = dj_models.CharField(
        choices=[("draft", "Draft"), ("paid", "Paid")],
    )
    direct.name = "status"
    related = dj_models.CharField(
        choices=[("open", "Open"), ("closed", "Closed")],
    )
    related.name = "status"  # same leaf name, different vocabulary

    e_direct = _choices_enum_for(direct, "W1Regress", "status")
    e_related = _choices_enum_for(
        related, "W1Regress", "shipment__status",
    )

    assert e_direct is not e_related
    assert {m.name for m in e_direct} == {"DRAFT", "PAID"}
    assert {m.name for m in e_related} == {"OPEN", "CLOSED"}
    # The single-segment axis keeps the historical (pre-§-6.2) type name.
    assert e_direct.__name__ == "W1RegressStatus"
    assert e_related.__name__ == "W1RegressShipmentStatus"
