"""What a field is to this library, for the schema and the compiler alike.

SPEC § 5 ("Which type a field is") and § 7. A subclass of a Django
temporal or numeric field that stores and returns that field's values
aggregates as it: its group key type, its date buckets, its SUM and AVG
types and its default operators. Every other field answers by its class
name, as before: a subclass that converts the values it reads (an enum
member, an opaque token), a string, UUID or duration subclass, a
``GeneratedField`` of any output, Django's own ``OrderWrt`` and a reverse
relation. The compiler reads the same answer (``field_kind``), so no
bucket is advertised that a query is refused.

Like ``test_builder_integration.py``, this module omits
``from __future__ import annotations``: the Query class annotates with
the live built types.
"""

import dataclasses
import datetime
import decimal
import re

import pytest
import strawberry
from django.contrib.contenttypes.fields import GenericForeignKey

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateOp,
    TimeGranularity,
    compute_aggregation,
)
from strawberry_django_aggregates.errors import GranularityNotApplicable
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
from tests.models import (
    Color,
    Contact,
    Customer,
    Ledger,
    Order,
    Reading,
    Span,
    Token,
    Volume,
)

UTC = datetime.UTC


def _schema(model, **options):
    built = AggregateBuilder(model=model, **options).build()
    namespace = {
        "__annotations__": {
            "agg": built.aggregate_type,
            "grouped": built.grouped_result_type,
        },
        "agg": built.aggregate_field,
        "grouped": built.group_by_field,
    }
    query = strawberry.type(type("Query", (), namespace))
    return strawberry.Schema(query=query)


def _type_block(sdl, name):
    found = re.search(rf"type {name} \{{(.*?)\}}", sdl, re.S)
    return found.group(1) if found else ""


def _key_fields(model, *names):
    key, _grouped, _result = make_grouped_type(
        model, group_by_fields=list(names),
    )
    return {field.name: field.type for field in dataclasses.fields(key)}


# ---------------------------------------------------------------------------
# what the release is for: a subclass returning its base field's values
# ---------------------------------------------------------------------------


def test_a_subclass_returning_its_base_values_aggregates_as_the_base():
    stamped, amount = (
        Reading._meta.get_field(name) for name in ("stamped_at", "amount")
    )

    assert field_type_for(stamped) == "DateTimeField"
    assert field_type_for(amount) == "DecimalField"
    assert field_type_for(Ledger._meta.get_field("units")) == "IntegerField"
    fields = _key_fields(Reading, "stamped_at", "amount")
    assert fields["stamped_at"] == datetime.datetime | None
    assert "stamped_at_month" in fields and "stamped_at_month_range" in fields
    assert fields["amount"] == decimal.Decimal | None
    assert _aggregate_python_type(AggregateOp.SUM, amount) is decimal.Decimal
    assert _aggregate_python_type(AggregateOp.AVG, amount) is decimal.Decimal
    assert _allowed_ops_for(Reading, "stamped_at", {}) == (
        default_operators_for("DateTimeField")
    )


@pytest.mark.django_db
def test_a_subclassed_datetime_groups_by_month_end_to_end():
    for month, day in ((4, 3), (4, 20), (5, 2)):
        moment = datetime.datetime(2026, month, day, 12, 0, tzinfo=UTC)
        Reading.objects.create(
            taken_at=moment, stamped_at=moment, amount=decimal.Decimal("2.50"),
        )
    schema = _schema(
        Reading, aggregate_fields=["amount"], group_by_fields=["stamped_at"],
    )

    result = schema.execute_sync("""
        query {
          grouped(groupBy: [{ field: STAMPED_AT, granularity: MONTH }]) {
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
        for row in result.data["grouped"]["results"]
    }
    assert rows == {
        "2026-04": (2, decimal.Decimal("5.00")),
        "2026-05": (1, decimal.Decimal("2.50")),
    }


# ---------------------------------------------------------------------------
# a subclass converting the values it reads keeps its class name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "name"),
    [
        (Ledger, "color"),
        (Ledger, "ref"),
        (Ledger, "token"),
        (Reading, "logged_at"),
    ],
)
def test_a_subclass_converting_its_values_keeps_its_class_name(model, name):
    field = model._meta.get_field(name)

    assert field_type_for(field) == type(field).__name__
    assert _allowed_ops_for(model, name, {}) == ()
    assert _key_fields(model, name)[name] == str | None
    assert f"{name}_month" not in _key_fields(model, name)
    assert name not in _resolve_aggregate_fields(model, None)


@pytest.mark.django_db
def test_an_opaque_or_enum_value_groups_as_the_string_it_returns():
    Ledger.objects.create(color=Color.RED, ref=Token(41))
    Ledger.objects.create(color=Color.BLUE, ref=Token(42))
    schema = _schema(Ledger)
    sdl = schema.as_str()

    refs = schema.execute_sync(
        "{ grouped(groupBy: [{field: REF}]) { results { key { ref } } } }",
    )
    colors = schema.execute_sync(
        "{ grouped(groupBy: [{field: COLOR}]) { results { key { color } } } }",
    )

    assert refs.errors is None and colors.errors is None
    ref_keys = sorted(r["key"]["ref"] for r in refs.data["grouped"]["results"])
    assert ref_keys == [str(Token(41)), str(Token(42))]
    assert sorted(
        r["key"]["color"] for r in colors.data["grouped"]["results"]
    ) == sorted([str(Color.RED), str(Color.BLUE)])
    key = _type_block(sdl, "LedgerGroupKey")
    assert "ref: String" in key and "color: String" in key
    assert "token" not in _type_block(sdl, "LedgerArrayAggFields")
    assert "ref" not in _type_block(sdl, "LedgerSumFields")


# ---------------------------------------------------------------------------
# the compiler reads the same owner
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_generated_date_column_has_no_bucket_and_the_compiler_agrees():
    moment = datetime.datetime(2026, 4, 3, 12, 0, tzinfo=UTC)
    Reading.objects.create(
        taken_at=moment, stamped_at=moment, amount=decimal.Decimal("1"),
    )
    mirrored = Reading._meta.get_field("mirrored_at")
    schema = _schema(Reading)

    assert field_type_for(mirrored) == "GeneratedField"
    assert "mirroredAtMonth" not in _type_block(
        schema.as_str(), "ReadingGroupKey",
    )
    refused = schema.execute_sync(
        "{ grouped(groupBy: [{field: MIRRORED_AT}]) "
        "{ results { key { mirroredAtMonth } count } } }",
    )
    assert refused.errors and "mirroredAtMonth" in refused.errors[0].message
    plain = schema.execute_sync(
        "{ grouped(groupBy: [{field: MIRRORED_AT}]) "
        "{ results { key { mirroredAt } count } } }",
    )
    assert plain.errors is None, plain.errors
    with pytest.raises(GranularityNotApplicable):
        compute_aggregation(
            Reading.objects.all(),
            aggregates=[(AggregateOp.COUNT, None)],
            group_by=[("mirrored_at", TimeGranularity.MONTH)],
        )


@pytest.mark.django_db
def test_the_compiler_truncates_every_stored_date_column_as_before():
    """A date subclass converting its values still truncates in SQL."""
    for day in (3, 20):
        moment = datetime.datetime(2026, 4, day, 12, 0, tzinfo=UTC)
        Reading.objects.create(
            taken_at=moment, stamped_at=moment, logged_at=moment,
            amount=decimal.Decimal("1"),
        )

    for name in ("stamped_at", "logged_at"):
        rows = compute_aggregation(
            Reading.objects.all(),
            aggregates=[(AggregateOp.COUNT, None)],
            group_by=[(name, TimeGranularity.MONTH)],
        )
        assert [row["count"] for row in rows] == [2], name


# ---------------------------------------------------------------------------
# what nobody asked for answers as before
# ---------------------------------------------------------------------------


def test_a_duration_subclass_and_a_generated_duration_build_as_before():
    sdl = _schema(Span).as_str()

    assert "lap: String" in _type_block(sdl, "SpanGroupKey")
    assert "length: String" in _type_block(sdl, "SpanGroupKey")
    lap, length = (Span._meta.get_field(n) for n in ("lap", "length"))
    assert field_type_for(lap) == "ElapsedField"
    assert field_type_for(length) == "GeneratedField"


def test_django_s_own_order_column_answers_by_its_class_name():
    sdl = _schema(Volume).as_str()

    assert "Order" not in _type_block(sdl, "VolumeSumFields")
    assert "Order: String" in _type_block(sdl, "VolumeGroupKey")
    assert "_order" not in _resolve_aggregate_fields(Volume, None)
    assert field_type_for(Volume._meta.get_field("_order")) == "OrderWrt"


def test_a_reverse_relation_and_a_generic_key_have_no_default_operator():
    sdl = _schema(Customer, aggregate_fields=["orders", "name"]).as_str()

    assert "orders" not in _type_block(sdl, "CustomerArrayAggFields")
    assert _allowed_ops_for(Customer, "orders", {}) == ()
    assert field_type_for(Customer._meta.get_field("orders")) == "ManyToOneRel"
    assert field_type_for(GenericForeignKey()) == "GenericForeignKey"


@pytest.mark.parametrize("name", ["email", "url", "slug"])
def test_django_string_fields_keep_their_operators_and_subclasses_none(name):
    tagged = f"tagged_{name}"

    assert _allowed_ops_for(Contact, name, {}) == default_operators_for(
        type(Contact._meta.get_field(name)).__name__,
    )
    assert _allowed_ops_for(Contact, name, {})
    assert _allowed_ops_for(Contact, tagged, {}) == ()
    allowed = (AggregateOp.MIN, AggregateOp.MAX)
    assert _allowed_ops_for(Contact, tagged, {tagged: allowed}) == allowed


def test_a_text_subclass_gains_operators_only_when_allowed():
    allowed = (AggregateOp.MIN, AggregateOp.MAX)

    assert field_type_for(Reading._meta.get_field("note")) == "SealedTextField"
    assert _allowed_ops_for(Reading, "note", {}) == ()
    assert _allowed_ops_for(Reading, "note", {"note": allowed}) == allowed
    assert field_type_for(Order._meta.get_field("status")) == "CharField"


@pytest.mark.parametrize(
    ("model", "name", "kind"),
    [
        (Reading, "stamped_at", ("DateTimeField", "DateTimeField")),
        (Reading, "amount", ("DecimalField", "DecimalField")),
        (Reading, "logged_at", ("DateTimeField", "LoggedAtField")),
        (Reading, "mirrored_at", (None, "GeneratedField")),
        (Reading, "note", (None, "SealedTextField")),
        (Ledger, "ref", ("IntegerField", "OpaqueIdField")),
        (Ledger, "token", (None, "TokenUUIDField")),
        (Ledger, "units", ("IntegerField", "IntegerField")),
        (Span, "lap", (None, "ElapsedField")),
        (Volume, "_order", ("IntegerField", "OrderWrt")),
        (Customer, "orders", (None, "ManyToOneRel")),
        (Customer, "id", ("BigIntegerField", "BigAutoField")),
    ],
)
def test_one_owner_answers_the_stored_type_and_the_aggregated_type(
    model, name, kind,
):
    """The compiler reads ``stored``, the schema ``name``: one decision."""
    from strawberry_django_aggregates.operators import field_kind

    assert tuple(field_kind(model._meta.get_field(name))) == kind
