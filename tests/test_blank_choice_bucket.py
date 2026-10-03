"""Blank-allowed choices group into an explicit, filterable enum bucket.

No postponed annotations: Strawberry needs the live generated types.
"""

import pytest
import strawberry
import strawberry_django

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateOp,
    ChoicesValueNotInEnumError,
    compute_aggregation,
    decode_group_cursor,
)
from tests.models import ChoiceRecord

pytestmark = pytest.mark.django_db


@strawberry_django.filter_type(ChoiceRecord, lookups=True)
class ChoiceRecordFilter:
    country: strawberry.auto


@strawberry.input
class ChoiceScalarLookup:
    """A consumer's scalar facet exposes exact matching as ``_eq``."""

    exact: str | None = strawberry.field(name="_eq", default=strawberry.UNSET)


@strawberry_django.filter_type(ChoiceRecord, lookups=True)
class ChoiceScalarFilter:
    country: ChoiceScalarLookup | None


@strawberry_django.type(ChoiceRecord)
class ChoiceRecordRow:
    id: strawberry.auto


def _schema(pagination_style="offset", filter_type=ChoiceRecordFilter):
    builder = AggregateBuilder(
        model=ChoiceRecord,
        aggregate_fields=[],
        group_by_fields=["country"],
        filter_type=filter_type,
        enable_filter_echo=filter_type is ChoiceRecordFilter,
        pagination_style=pagination_style,
    )
    built = builder.build()
    result_type = (
        built.grouped_result_type if pagination_style == "offset"
        else built.grouped_connection_type
    )
    group_field = (
        built.group_by_field if pagination_style == "offset"
        else built.grouped_connection_field
    )

    @strawberry.type
    class Query:
        records_group_by: result_type = group_field
        records: list[ChoiceRecordRow] = strawberry_django.field(
            filters=filter_type,
        )

    return builder, built, strawberry.Schema(query=Query)


@pytest.fixture
def choice_records():
    blank = [ChoiceRecord.objects.create(),
             ChoiceRecord.objects.create(country="")]
    null = [ChoiceRecord.objects.create(country=None)]
    cz = [ChoiceRecord.objects.create(country="CZ") for _ in range(3)]
    return {"": blank, None: null, "CZ": cz}


@pytest.mark.parametrize("pagination_style", ["offset", "cursor"])
def test_blank_and_null_buckets_filter_back_separately(
    choice_records, pagination_style,
):
    _, _, schema = _schema(pagination_style)
    fields = "key { country } count filter"
    selection = (
        "results { " + fields + " }" if pagination_style == "offset"
        else "edges { cursor node { " + fields + " } }"
    )
    grouped = schema.execute_sync(
        "{ recordsGroupBy(groupBy: [{ field: COUNTRY }]) { "
        + selection + " totalCount } }",
    )
    assert grouped.errors is None, grouped.errors
    payload = grouped.data["recordsGroupBy"]
    assert payload["totalCount"] == 3
    buckets = (
        payload["results"] if pagination_style == "offset"
        else [edge["node"] for edge in payload["edges"]]
    )
    by_country = {bucket["key"]["country"]: bucket for bucket in buckets}
    assert set(by_country) == {"BLANK", None, "CZ"}
    assert by_country["BLANK"] == {
        "key": {"country": "BLANK"},
        "count": 2,
        "filter": {"country": {"exact": ""}},
    }
    assert by_country[None] == {
        "key": {"country": None},
        "count": 1,
        "filter": {"country": {"isNull": True}},
    }
    assert by_country["CZ"]["count"] == 3

    for wire, stored in [("BLANK", ""), (None, None), ("CZ", "CZ")]:
        replayed = schema.execute_sync(
            "query($f: ChoiceRecordFilter!) { records(filters: $f) { id } }",
            variable_values={"f": by_country[wire]["filter"]},
        )
        assert replayed.errors is None, replayed.errors
        assert {int(row["id"]) for row in replayed.data["records"]} == {
            record.pk for record in choice_records[stored]
        }

    if pagination_style == "cursor":
        blank_edge = next(
            edge for edge in payload["edges"]
            if edge["node"]["key"]["country"] == "BLANK"
        )
        assert decode_group_cursor(blank_edge["cursor"]) == [""]


def test_blank_key_round_trips_through_scalar_eq_filter(choice_records):
    builder, built, schema = _schema(filter_type=ChoiceScalarFilter)
    spec = [("country", None)]
    rows = compute_aggregation(
        ChoiceRecord.objects.all(),
        group_by=spec,
        aggregates=[(AggregateOp.COUNT, None)],
    )
    blank_row = next(row for row in rows if row["country"] == "")
    blank_key = builder.shape_group_key(built.group_key_type, blank_row, spec)
    assert blank_key.country.name == "BLANK"
    assert blank_key.country.value == ""
    assert blank_row == {"country": "", "count": 2}
    null_key = builder.shape_group_key(
        built.group_key_type, {"country": None}, spec,
    )
    assert null_key.country is None

    drill_down = {"country": {"_eq": blank_key.country.value}}
    assert drill_down == {"country": {"_eq": ""}}
    replayed = schema.execute_sync(
        "query($f: ChoiceScalarFilter!) { "
        "records(filters: $f) { id } "
        "recordsGroupBy(filter: $f, groupBy: [{ field: COUNTRY }]) { "
        "results { key { country } count } totalCount } }",
        variable_values={"f": drill_down},
    )
    assert replayed.errors is None, replayed.errors
    assert {int(row["id"]) for row in replayed.data["records"]} == {
        record.pk for record in choice_records[""]
    }
    assert replayed.data["recordsGroupBy"] == {
        "results": [{"key": {"country": "BLANK"}, "count": 2}],
        "totalCount": 1,
    }


def test_blank_choices_schema_is_deterministic():
    _, _, first = _schema()
    _, _, second = _schema()
    assert first.as_str() == second.as_str()
    assert "country: ChoiceRecordCountry" in first.as_str()
    assert "enum ChoiceRecordCountry {\n  CZ\n  US\n  BLANK\n}" in (
        first.as_str()
    )


@pytest.mark.parametrize("value", ["XX", " "])
def test_blank_allowed_choices_still_reject_invalid_values(value):
    ChoiceRecord.objects.create(country=value)
    builder, built, schema = _schema()
    with pytest.raises(ChoicesValueNotInEnumError, match="country"):
        builder.shape_group_key(
            built.group_key_type, {"country": value}, [("country", None)],
        )
    grouped = schema.execute_sync(
        "{ recordsGroupBy(groupBy: [{ field: COUNTRY }]) { "
        "results { key { country } count } } }",
    )
    assert grouped.errors is not None
    assert isinstance(
        grouped.errors[0].original_error, ChoicesValueNotInEnumError,
    )
