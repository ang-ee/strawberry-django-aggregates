"""Choices-backed group-by columns emit GraphQL enums on the group key.

A ``choices`` field on ``<Model>GroupKey`` must surface as a typed
GraphQL enum (``OrderStatus``), not its base scalar (``String`` / ``Int``),
and the grouped resolver must coerce the raw stored row value to the
matching enum member on output.

Covered:

- SDL: ``OrderGroupKey.status`` is the ``OrderStatus`` enum (not
  ``String``) with members ``DRAFT`` / ``PAID`` / ``CANCELLED`` whose
  values are ``draft`` / ``paid`` / ``cancelled``.
- Round-trip: ``groupBy: [{ field: STATUS }]`` serializes ``key.status``
  as the enum member NAME and returns correct counts.
- Determinism: two builds emit byte-identical SDL for the enum + key.
- Plain integer ``choices`` (``TaskPriority``) derive member names from
  labels; a plain ``IntegerField`` with no choices keeps its scalar.
- django-choices-field ``choices_enum`` (name != value) preserves member
  NAMES. The library reads ``choices_enum`` via ``getattr`` and does NOT
  depend on django-choices-field, so the branch is exercised by attaching
  a ``choices_enum`` to a plain field — exactly the shape
  django-choices-field exposes.
- Name collision fails loud with ``ChoicesEnumCollisionError``.

Like ``test_builder_integration.py`` / ``test_determinism.py``, this
module deliberately omits ``from __future__ import annotations`` — under
PEP 563 the dynamic ``built.*`` field-type annotations on the ``Query``
class become strings Strawberry cannot evaluate.
"""

import enum
import typing

import pytest
import strawberry
from django.db import models

from strawberry_django_aggregates import AggregateBuilder
from strawberry_django_aggregates.errors import (
    ChoicesEnumCollisionError,
    ChoicesEnumNameError,
    ChoicesValueNotInEnumError,
)
from strawberry_django_aggregates.types import _choices_enum_for
from tests.models import Customer, Order, Task

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _unwrap_enum(annotated):
    """Return the enum class from an ``EnumType | None`` annotation."""
    for arg in typing.get_args(annotated) or (annotated,):
        if isinstance(arg, type) and issubclass(arg, enum.Enum):
            return arg
    raise AssertionError(f"no enum in annotation {annotated!r}")


def _order_status_built():
    return AggregateBuilder(
        model=Order,
        aggregate_fields=["total"],
        group_by_fields=["status", "created_at"],
    ).build()


def _order_status_sdl() -> str:
    built = _order_status_built()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query).as_str()


def _enum_block(sdl: str, header: str) -> str:
    return next(b for b in sdl.split("\n\n") if b.startswith(header))


# ---------------------------------------------------------------------------
# SDL emission — Order.status (plain str choices)
# ---------------------------------------------------------------------------

def test_group_key_status_is_enum_not_string(db):
    sdl = _order_status_sdl()
    assert "status: OrderStatus" in sdl, sdl
    # The base scalar must NOT leak through for the choices column.
    assert "status: String" not in sdl, sdl


def test_status_enum_members_and_values(db):
    built = _order_status_built()

    sdl = _order_status_sdl()
    enum_block = _enum_block(sdl, "enum OrderStatus")
    for name in ("DRAFT", "PAID", "CANCELLED"):
        assert name in enum_block, enum_block

    # Member NAME -> stored value mapping, verbatim from the choices.
    status_type = built.group_key_type.__annotations__["status"]
    members = {m.name: m.value for m in _unwrap_enum(status_type)}
    assert members == {
        "DRAFT": "draft",
        "PAID": "paid",
        "CANCELLED": "cancelled",
    }


def test_non_choices_columns_keep_scalar(db):
    sdl = _order_status_sdl()
    # A date column still emits DateTime, not an enum.
    assert "createdAt: DateTime" in sdl, sdl


# ---------------------------------------------------------------------------
# Round-trip execution — raw value coerced to the enum member NAME
# ---------------------------------------------------------------------------

def test_group_by_status_round_trip(sample_orders):
    built = _order_status_built()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    schema = strawberry.Schema(query=Query)
    result = schema.execute_sync(
        """
        query {
            ordersGroupBy(groupBy: [{ field: STATUS }]) {
                results {
                    key { status }
                    count
                }
            }
        }
        """,
    )
    assert result.errors is None, result.errors
    rows = result.data["ordersGroupBy"]["results"]
    by_status = {r["key"]["status"]: r["count"] for r in rows}
    # sample_orders: 4 paid, 1 cancelled, 1 draft. The key is serialized
    # as the enum member NAME, not the raw "paid"/"draft" stored value.
    assert by_status == {"PAID": 4, "CANCELLED": 1, "DRAFT": 1}


# ---------------------------------------------------------------------------
# Determinism — byte-identical SDL across two builds
# ---------------------------------------------------------------------------

def test_choices_enum_sdl_is_deterministic(db):
    sdl_1 = _order_status_sdl()
    sdl_2 = _order_status_sdl()
    assert sdl_1 == sdl_2, "choices-enum SDL is non-deterministic"


def test_choices_enum_is_cached_same_object(db):
    """Repeated builds return the SAME Strawberry enum object — required
    for determinism and to avoid Strawberry duplicate-type errors.
    """
    field = Order._meta.get_field("status")
    e1 = _choices_enum_for(field, "Order")
    e2 = _choices_enum_for(field, "Order")
    assert e1 is e2


# ---------------------------------------------------------------------------
# Integer choices — label-derived member names; non-choices Int unchanged
# ---------------------------------------------------------------------------

def _task_built():
    return AggregateBuilder(
        model=Task,
        aggregate_fields=["effort"],
        group_by_fields=["priority", "effort"],
    ).build()


def _task_sdl() -> str:
    built = _task_built()

    @strawberry.type
    class Query:
        tasks_group_by: built.grouped_result_type = built.group_by_field

    return strawberry.Schema(query=Query).as_str()


def test_integer_choices_derive_names_from_labels(db):
    """Plain integer ``choices`` cannot use digit-leading member names,
    so the names come from the labels (``1 / "Low"`` -> ``LOW``).
    """
    built = _task_built()
    priority_type = built.group_key_type.__annotations__["priority"]
    members = {m.name: m.value for m in _unwrap_enum(priority_type)}
    assert members == {"LOW": 1, "MEDIUM": 2, "HIGH": 3}

    sdl = _task_sdl()
    assert "priority: TaskPriority" in sdl, sdl
    # ``effort`` is a plain IntegerField with NO choices — stays Int.
    assert "effort: Int" in sdl, sdl


def test_integer_choices_round_trip(db):
    Task.objects.create(priority=1, effort=5)
    Task.objects.create(priority=1, effort=3)
    Task.objects.create(priority=3, effort=8)

    built = _task_built()

    @strawberry.type
    class Query:
        tasks_group_by: built.grouped_result_type = built.group_by_field

    schema = strawberry.Schema(query=Query)
    result = schema.execute_sync(
        """
        query {
            tasksGroupBy(groupBy: [{ field: PRIORITY }]) {
                results {
                    key { priority }
                    count
                }
            }
        }
        """,
    )
    assert result.errors is None, result.errors
    rows = result.data["tasksGroupBy"]["results"]
    by_priority = {r["key"]["priority"]: r["count"] for r in rows}
    assert by_priority == {"LOW": 2, "HIGH": 1}


# ---------------------------------------------------------------------------
# django-choices-field ``choices_enum`` (name != value) — getattr-driven
# ---------------------------------------------------------------------------
#
# The library never imports django-choices-field; it only reads a
# ``choices_enum`` attribute when present (exactly what
# ``TextChoicesField`` / ``IntegerChoicesField`` expose). We simulate
# that shape by attaching ``choices_enum`` to a plain field so the test
# stays dependency-free.

def test_choices_enum_attr_preserves_member_names(db):
    class State(enum.Enum):
        DRAFT = "draft"
        IN_PROGRESS = "wip"
        DONE = "done"

    field = models.CharField(
        max_length=8, choices=[(m.value, m.name) for m in State],
    )
    field.name = "state"
    field.choices_enum = State  # what django-choices-field exposes

    built = _choices_enum_for(field, "Ticket")
    assert built is not None
    members = {m.name: m.value for m in built}
    # Member NAMES are reused verbatim — ``IN_PROGRESS`` survives even
    # though its value ``"wip"`` would otherwise sanitize to ``WIP``.
    assert members == {
        "DRAFT": "draft",
        "IN_PROGRESS": "wip",
        "DONE": "done",
    }
    assert built.__name__ == "TicketState"


# ---------------------------------------------------------------------------
# Fail-loud on member-name collision
# ---------------------------------------------------------------------------

def test_member_name_collision_raises(db):
    # ``"a-b"`` and ``"a/b"`` both sanitize to ``A_B``.
    field = models.CharField(
        max_length=8, choices=[("a-b", "X"), ("a/b", "Y")],
    )
    field.name = "kind"
    with pytest.raises(ChoicesEnumCollisionError):
        _choices_enum_for(field, "Collide")


def test_duplicate_stored_value_raises(db):
    """Two choices sharing a stored value would make ``enum.Enum``
    silently alias the second to the first, dropping it from the schema.
    Refuse loud instead of emitting a partial enum.
    """
    field = models.IntegerField(choices=[(1, "Low"), (1, "Cheap")])
    field.name = "grade"
    with pytest.raises(ChoicesEnumCollisionError):
        _choices_enum_for(field, "Dup")


# ---------------------------------------------------------------------------
# Fail-loud when no legal member name can be derived
# ---------------------------------------------------------------------------

def test_unresolvable_member_name_raises(db):
    """When BOTH the value and the label sanitize to an empty or
    digit-leading name (``1 / "1st"``), no legal enum member name exists.
    ``enum.Enum`` would raise a bare ``ValueError``; we raise the typed
    :class:`ChoicesEnumNameError` instead.
    """
    field = models.IntegerField(choices=[(1, "1st"), (2, "2nd")])
    field.name = "rank"
    with pytest.raises(ChoicesEnumNameError):
        _choices_enum_for(field, "Race")


# ---------------------------------------------------------------------------
# Fail-loud when a stored row value is outside the declared choices
# ---------------------------------------------------------------------------

def test_out_of_choices_value_raises(db):
    """``choices`` is a Django validation concern, not a DB constraint, so
    a column may legally hold a value no longer in the choices list. The
    resolver coerces grouped rows to the emitted enum; an out-of-choices
    value fails loud with :class:`ChoicesValueNotInEnumError` (naming the
    field, value, and enum) rather than a bare ``ValueError``.
    """
    import datetime

    customer = Customer.objects.create(name="Legacy")
    # ``create`` does not run model validation — Django inserts the row.
    Order.objects.create(
        customer=customer, status="legacy",
        created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
    )

    built = _order_status_built()

    @strawberry.type
    class Query:
        orders_group_by: built.grouped_result_type = built.group_by_field

    schema = strawberry.Schema(query=Query)
    result = schema.execute_sync(
        """
        query {
            ordersGroupBy(groupBy: [{ field: STATUS }]) {
                results { key { status } count }
            }
        }
        """,
    )
    assert result.errors is not None, "expected the resolver to fail loud"
    original = result.errors[0].original_error
    assert isinstance(original, ChoicesValueNotInEnumError), original
    assert "legacy" in str(original)
    assert "OrderStatus" in str(original)
