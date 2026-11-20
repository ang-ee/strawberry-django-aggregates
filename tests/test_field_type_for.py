"""A field aggregates as the Django field type it stores (SPEC § 5, § 7).

A subclass of a Django field keeps its column, its SQL and its Python
values, and ``GeneratedField`` stores its ``output_field``'s type, so
both aggregate as that type: the group key's GraphQL type, the
per-granularity date buckets, SUM and AVG result types, and the default
operator allowlist. String kinds keep the exact class name, so a text
subclass gains no default MIN, MAX, MODE, ARRAY_AGG or STRING_AGG and
opts in through ``operators=``.

Like ``test_builder_integration.py``, this module omits
``from __future__ import annotations``: the Query class annotates with
the live built types.
"""

import dataclasses
import datetime
import decimal

import pytest
import strawberry
from django.contrib.contenttypes.fields import GenericForeignKey

from strawberry_django_aggregates import AggregateBuilder, AggregateOp
from strawberry_django_aggregates.operators import (
    default_operators_for,
    field_type_for,
)
from strawberry_django_aggregates.types import (
    _aggregate_python_type,
    _allowed_ops_for,
    _resolve_aggregate_fields,
    make_grouped_type,
)
from tests.models import Order, Reading

TEMPORAL = ("taken_at", "stamped_at", "mirrored_at")


def _field(name):
    return Reading._meta.get_field(name)


def test_a_field_aggregates_as_its_stored_type_a_string_as_its_class():
    assert [field_type_for(_field(name)) for name in TEMPORAL] == [
        "DateTimeField",
    ] * 3
    assert field_type_for(_field("amount")) == "DecimalField"
    assert field_type_for(_field("note")) == "SealedTextField"
    # Django's own string subclasses keep their own names, as before.
    assert field_type_for(Order._meta.get_field("status")) == "CharField"
    assert field_type_for(GenericForeignKey()) == "GenericForeignKey"


def test_the_group_key_types_each_temporal_kind_with_its_buckets():
    key, _grouped, _result = make_grouped_type(
        Reading, group_by_fields=[*TEMPORAL, "amount"],
    )
    fields = {field.name: field.type for field in dataclasses.fields(key)}

    for name in TEMPORAL:
        assert fields[name] == datetime.datetime | None, name
        assert f"{name}_month" in fields, name
        assert f"{name}_month_range" in fields, name
        assert f"{name}_day_of_week" in fields, name
    assert fields["amount"] == decimal.Decimal | None


def test_sum_avg_min_and_max_take_the_stored_types():
    amount, stamped = _field("amount"), _field("stamped_at")

    assert _aggregate_python_type(AggregateOp.SUM, amount) is decimal.Decimal
    assert _aggregate_python_type(AggregateOp.AVG, amount) is decimal.Decimal
    assert _aggregate_python_type(AggregateOp.MAX, amount) is decimal.Decimal
    assert _aggregate_python_type(AggregateOp.MIN, stamped) is (
        datetime.datetime
    )


def test_default_operators_follow_the_stored_type_never_a_text_kind():
    for name in TEMPORAL:
        assert _allowed_ops_for(Reading, name, {}) == default_operators_for(
            "DateTimeField",
        ), name
    assert _allowed_ops_for(Reading, "amount", {}) == default_operators_for(
        "DecimalField",
    )
    assert _allowed_ops_for(Reading, "note", {}) == ()
    assert "note" not in _resolve_aggregate_fields(Reading, None)
    assert {"stamped_at", "amount", "mirrored_at"} <= set(
        _resolve_aggregate_fields(Reading, None),
    )

    allowed = (AggregateOp.MIN, AggregateOp.MAX)
    assert _allowed_ops_for(Reading, "note", {"note": allowed}) == allowed


@pytest.mark.django_db
def test_a_subclassed_datetime_groups_by_month_end_to_end():
    utc = datetime.UTC
    for month, day in ((4, 3), (4, 20), (5, 2)):
        moment = datetime.datetime(2026, month, day, 12, 0, tzinfo=utc)
        Reading.objects.create(
            taken_at=moment, stamped_at=moment, amount=decimal.Decimal("2.50"),
        )
    built = AggregateBuilder(
        model=Reading,
        aggregate_fields=["amount"],
        group_by_fields=["stamped_at"],
    ).build()

    @strawberry.type
    class Query:
        readings_group_by: built.grouped_result_type = built.group_by_field

    result = strawberry.Schema(query=Query).execute_sync("""
        query {
          readingsGroupBy(groupBy: [
            { field: STAMPED_AT, granularity: MONTH }
          ]) {
            results {
              key { stampedAtMonth stampedAtMonthRange { from to } }
              count
              sum { amount }
            }
          }
        }
    """)

    assert result.errors is None, result.errors
    rows = {
        row["key"]["stampedAtMonth"][:7]: (
            row["count"], decimal.Decimal(row["sum"]["amount"]),
        )
        for row in result.data["readingsGroupBy"]["results"]
    }
    assert rows == {
        "2026-04": (2, decimal.Decimal("5.00")),
        "2026-05": (1, decimal.Decimal("2.50")),
    }
