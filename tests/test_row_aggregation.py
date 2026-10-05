"""Grouping computed rows — ``make_row_model`` + ``compute_row_aggregation``.

The row path must be indistinguishable from the queryset path: the same
types from the same builder, the same flat result rows from the same
inputs, and the same fail-loud errors. Parity tests run both paths over
the same orders and compare. SPEC § 20.

No postponed annotations: Strawberry needs the live generated types.
"""

import dataclasses
import datetime
import decimal
import enum
import re
import types
import uuid

import pytest
import strawberry
from django.apps import apps

from strawberry_django_aggregates import (
    AggregateBuilder,
    AggregateError,
    AggregateOp,
    GranularityNotApplicable,
    GroupByFieldNotAllowed,
    HavingFieldNotAllowed,
    NumberGranularity,
    OperatorNotSupportedError,
    OrderFieldNotAllowed,
    TimeGranularity,
    compute_aggregation,
    compute_row_aggregation,
    make_row_model,
)
from tests.models import Order

COUNT = [(AggregateOp.COUNT, None)]


class Kind(enum.Enum):
    FIELD = "field"
    RELATION = "relation"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


def _order_rows_model():
    return make_row_model(
        "OrderRow",
        {
            "status": str,
            "quantity": int,
            "is_priority": bool,
            "total": decimal.Decimal,
            "created_at": datetime.datetime,
        },
    )


def _order_rows():
    return list(
        Order.objects.values(
            "status", "quantity", "is_priority", "total", "created_at",
        )
    )


# --- make_row_model


def test_row_model_is_abstract_and_never_registered():
    model = make_row_model("ComputedThing", {"name": str, "model": str})

    assert model._meta.abstract
    assert [f.name for f in model._meta.get_fields()] == ["name", "model"]
    registered = {m.__name__ for m in apps.get_models()}
    assert "ComputedThing" not in registered


@pytest.mark.parametrize(("python_type", "field_class"), [
    (bool, "BooleanField"),
    (int, "IntegerField"),
    (float, "FloatField"),
    (decimal.Decimal, "DecimalField"),
    (str, "CharField"),
    (datetime.datetime, "DateTimeField"),
    (datetime.date, "DateField"),
    (datetime.time, "TimeField"),
    (uuid.UUID, "UUIDField"),
])
def test_row_model_maps_python_types_to_nullable_fields(
    python_type, field_class,
):
    field = make_row_model("Typed", {"value": python_type})._meta.get_field(
        "value",
    )

    assert type(field).__name__ == field_class
    assert field.null


def test_row_model_enum_column_is_a_choices_field():
    field = make_row_model("Kinds", {"kind": Kind})._meta.get_field("kind")

    assert type(field).__name__ == "CharField"
    assert list(field.choices) == [
        ("field", "FIELD"), ("relation", "RELATION"),
    ]
    assert field.choices_enum is Kind
    int_field = make_row_model("Levels", {"level": Level})._meta.get_field(
        "level",
    )
    assert type(int_field).__name__ == "IntegerField"


@pytest.mark.parametrize("columns", [
    {"bad name": str},
    {"_private": str},
    {"trailing_": str},
    {"a__b": str},
    {"pk": str},
    {"class": str},
    {"payload": dict},
    {"items": list[str]},
    {"mixed": enum.Enum("Mixed", {"A": "a", "B": 2})},
])
def test_row_model_rejects_unusable_columns(columns):
    with pytest.raises(TypeError):
        make_row_model("Rejected", columns)


def test_row_model_rejects_a_non_identifier_name():
    with pytest.raises(TypeError):
        make_row_model("computed rows", {"name": str})


# --- types: the same builder, the same SDL


def _group_sdl(builder):
    built = builder.build()

    @strawberry.type
    class Query:
        @strawberry.field
        def groups(
            self,
            group_by: list[built.group_by_spec],
            having: built.having_input | None = None,
            order_by: list[built.group_order_input] | None = None,
        ) -> list[built.group_key_type]:
            return []

    return strawberry.Schema(query=Query).as_str()


def _type_blocks(sdl):
    """Every named type block except the test's own Query."""
    return sorted(
        block
        for block in re.findall(
            r"^(?:type|input|enum) .*?^}", sdl, re.M | re.S,
        )
        if not block.startswith("type Query")
    )


def test_row_builder_emits_the_queryset_group_types():
    columns = ["quantity", "is_priority", "total", "created_at"]
    queryset_sdl = _group_sdl(AggregateBuilder(
        model=Order, name_prefix="Orders",
        aggregate_fields=[], group_by_fields=columns,
    ))
    row_sdl = _group_sdl(AggregateBuilder(
        model=_order_rows_model(), name_prefix="Orders",
        aggregate_fields=[], group_by_fields=columns,
    ))

    assert _type_blocks(row_sdl) == _type_blocks(queryset_sdl)
    assert "type OrdersGroupKey {" in row_sdl
    assert "createdAtMonthRange: BucketRange" in row_sdl


def test_row_builder_sdl_is_deterministic():
    def build():
        return _group_sdl(AggregateBuilder(
            model=make_row_model("Things", {"kind": Kind, "name": str}),
            name_prefix="Things",
            aggregate_fields=[],
            group_by_fields=["kind", "name"],
        ))

    assert build() == build()


def test_enum_column_key_reuses_the_enum_member_names():
    model = make_row_model("Fields", {"kind": Kind})
    builder = AggregateBuilder(
        model=model, name_prefix="Fields",
        aggregate_fields=[], group_by_fields=["kind"],
    )
    built = builder.build()
    spec = [("kind", None)]
    rows = compute_row_aggregation(
        [{"kind": Kind.RELATION}, {"kind": Kind.FIELD}, {"kind": None}],
        model=model, group_by=spec, aggregates=COUNT,
    )

    keys = [
        builder.shape_group_key(built.group_key_type, row, spec).kind
        for row in rows
    ]
    assert [row["kind"] for row in rows] == ["field", "relation", None]
    assert [key.name if key else None for key in keys] == [
        "FIELD", "RELATION", None,
    ]
    assert "enum FieldsKind {\n  FIELD\n  RELATION\n}" in _group_sdl(builder)


def test_builder_query_fields_refuse_a_row_model_without_queryset():
    built = AggregateBuilder(
        model=make_row_model("NoTable", {"name": str}),
        group_by_fields=["name"],
    ).build()

    @strawberry.type
    class Query:
        no_table_aggregate = built.aggregate_field

    result = strawberry.Schema(query=Query).execute_sync(
        "{ noTableAggregate { count } }",
    )

    assert result.errors is not None
    assert "compute_row_aggregation" in str(result.errors[0])


def test_row_model_builder_counts_only():
    model = make_row_model("Measured", {"size": int, "price": float})
    sdl = _group_sdl(AggregateBuilder(
        model=model, name_prefix="Measured", group_by_fields=["size"],
    ))

    assert "MeasuredSumFields" not in sdl
    assert "sumSizeGt" not in sdl
    assert "countGt: Int" in sdl
    for options in (
        {"aggregate_fields": ["size"]},
        {"json_paths": {"meta.region": "str"}},
    ):
        with pytest.raises(AggregateError, match="row model"):
            AggregateBuilder(
                model=model, group_by_fields=["size"], **options,
            ).build()


@pytest.mark.parametrize("group_by_fields", [["count"], None])
def test_row_model_builder_refuses_a_count_column(group_by_fields):
    model = make_row_model("Tallies", {"label": str, "count": int})

    with pytest.raises(AggregateError, match="count"):
        AggregateBuilder(
            model=model, group_by_fields=group_by_fields,
        ).build()


# --- execution parity with compute_aggregation


@pytest.mark.django_db
@pytest.mark.parametrize("group_by", [
    [("status", None)],
    [("is_priority", None), ("quantity", None)],
    [("total", None)],
])
def test_plain_axes_match_the_queryset_path(sample_orders, group_by):
    aliases = [field for field, _ in group_by]
    order_by = [(alias, "asc", None) for alias in aliases]

    expected = compute_aggregation(
        Order.objects.all(),
        group_by=group_by, aggregates=COUNT, order_by=order_by,
    )
    actual = compute_row_aggregation(
        _order_rows(), model=_order_rows_model(),
        group_by=group_by, aggregates=COUNT, order_by=order_by,
    )

    assert actual == expected


@pytest.mark.django_db
@pytest.mark.parametrize("granularity", [
    *TimeGranularity, *NumberGranularity,
])
@pytest.mark.parametrize("tz", ["UTC", "Asia/Tokyo", "America/New_York"])
@pytest.mark.parametrize("week_start", [1, 7])
def test_date_axes_match_the_queryset_path(
    sample_orders, granularity, tz, week_start,
):
    """TIME buckets and NUMBER parts wrap into ``tz`` before truncating."""
    if tz != "UTC" and granularity is NumberGranularity.DAY_OF_YEAR:
        pytest.skip("SQLite's day-of-year SQL ignores tz (SPEC § 7)")
    if tz != "UTC" and week_start != 1 and (
        granularity is TimeGranularity.WEEK
    ):
        pytest.skip("SQLite labels shifted week buckets as UTC")
    group_by = [("created_at", granularity)]
    alias = f"created_at_{granularity.value}"
    order_by = [(alias, "asc", None)]

    expected = compute_aggregation(
        Order.objects.all(), group_by=group_by, aggregates=COUNT,
        order_by=order_by, tz=tz, week_start=week_start,
    )
    actual = compute_row_aggregation(
        _order_rows(), model=_order_rows_model(), group_by=group_by,
        aggregates=COUNT, order_by=order_by, tz=tz, week_start=week_start,
    )

    def _wire(rows):
        return [
            {
                key: value.isoformat()
                if isinstance(value, datetime.date) else value
                for key, value in row.items()
            }
            for row in rows
        ]

    assert _wire(actual) == _wire(expected)


@pytest.mark.django_db
def test_date_axes_bucket_in_the_requested_timezone(sample_orders):
    """Pins the tz-correct values the SQLite SQL path cannot produce.

    2026-05-10 23:30 UTC is 2026-05-11 08:30 in Tokyo: day 131, a Monday,
    in the Sunday-start week of 2026-05-10 (Tokyo midnight).
    """
    tokyo = datetime.datetime(2026, 5, 10, 23, 30, tzinfo=datetime.UTC)
    rows = [{**row, "created_at": tokyo} for row in _order_rows()[:1]]

    def group(granularity, **arguments):
        return compute_row_aggregation(
            rows, model=_order_rows_model(), aggregates=COUNT,
            group_by=[("created_at", granularity)], tz="Asia/Tokyo",
            **arguments,
        )[0][f"created_at_{granularity.value}"]

    week = group(TimeGranularity.WEEK, week_start=7)
    assert group(NumberGranularity.DAY_OF_YEAR) == 131
    assert group(NumberGranularity.DAY_OF_WEEK) == 1
    assert group(NumberGranularity.DAY_OF_WEEK, week_start=7) == 2
    assert week.isoformat() == "2026-05-10T00:00:00+09:00"
    assert group(TimeGranularity.DAY).isoformat() == (
        "2026-05-11T00:00:00+09:00"
    )


@pytest.mark.django_db
def test_having_ordering_and_paging_match_the_queryset_path(sample_orders):
    group_by = [("quantity", None)]
    arguments = {
        "group_by": group_by,
        "aggregates": COUNT,
        "having": {"count__gte": 1, "count__not_in": [5]},
        "order_by": [("count", "desc", None), ("quantity", "asc", None)],
        "offset": 1,
        "limit": 2,
    }

    expected = compute_aggregation(Order.objects.all(), **arguments)
    actual = compute_row_aggregation(
        _order_rows(), model=_order_rows_model(), **arguments,
    )

    assert actual == expected


@pytest.mark.django_db
def test_no_group_by_returns_one_unpaged_row(sample_orders):
    expected = compute_aggregation(
        Order.objects.all(), aggregates=COUNT, offset=3, limit=1,
    )
    actual = compute_row_aggregation(
        _order_rows(), model=_order_rows_model(), aggregates=COUNT,
        offset=3, limit=1,
    )

    assert actual == expected == [{"count": 6}]
    assert compute_row_aggregation(
        [], model=_order_rows_model(), aggregates=COUNT,
    ) == [{"count": 0}]


# --- keys, nulls and ordering


@dataclasses.dataclass
class FieldRow:
    name: str
    relation_target: str | None
    created: datetime.date | None = None


def _field_model():
    return make_row_model(
        "FieldRows",
        {"name": str, "relation_target": str, "created": datetime.date},
    )


def test_null_and_empty_string_are_distinct_buckets():
    rows = [
        FieldRow("a", "auth.user"),
        FieldRow("b", ""),
        FieldRow("c", None),
        FieldRow("d", ""),
    ]

    result = compute_row_aggregation(
        rows, model=_field_model(),
        group_by=[("relation_target", None)], aggregates=COUNT,
    )

    assert result == [
        {"relation_target": "", "count": 2},
        {"relation_target": "auth.user", "count": 1},
        {"relation_target": None, "count": 1},
    ]


def test_default_order_is_the_key_tuple_regardless_of_row_order():
    rows = [
        FieldRow("b", "x"), FieldRow("a", None), FieldRow("a", "y"),
        FieldRow("b", "x"), FieldRow("a", "x"),
    ]
    spec = [("name", None), ("relation_target", None)]

    forward = compute_row_aggregation(
        rows, model=_field_model(), group_by=spec, aggregates=COUNT,
    )
    backward = compute_row_aggregation(
        list(reversed(rows)), model=_field_model(), group_by=spec,
        aggregates=COUNT,
    )

    assert forward == backward
    assert [(r["name"], r["relation_target"]) for r in forward] == [
        ("a", "x"), ("a", "y"), ("a", None), ("b", "x"),
    ]


@pytest.mark.parametrize(("direction", "nulls", "expected"), [
    ("asc", None, ["auth.user", "core.item", None]),
    ("desc", None, [None, "core.item", "auth.user"]),
    ("asc", "first", [None, "auth.user", "core.item"]),
    ("desc", "last", ["core.item", "auth.user", None]),
])
def test_order_by_places_nulls_like_the_sql_enum(direction, nulls, expected):
    rows = [
        FieldRow("a", "core.item"), FieldRow("b", None),
        FieldRow("c", "auth.user"),
    ]

    result = compute_row_aggregation(
        rows, model=_field_model(),
        group_by=[("relation_target", None)], aggregates=COUNT,
        order_by=[("relation_target", direction, nulls)],
    )

    assert [row["relation_target"] for row in result] == expected


def test_date_column_buckets_to_dates():
    rows = [
        FieldRow("a", None, datetime.date(2026, 5, 3)),
        FieldRow("b", None, datetime.date(2026, 5, 30)),
        FieldRow("c", None, datetime.date(2026, 6, 1)),
        FieldRow("d", None, None),
    ]

    result = compute_row_aggregation(
        rows, model=_field_model(),
        group_by=[("created", TimeGranularity.MONTH)], aggregates=COUNT,
    )

    assert result == [
        {"created_month": datetime.date(2026, 5, 1), "count": 2},
        {"created_month": datetime.date(2026, 6, 1), "count": 1},
        {"created_month": None, "count": 1},
    ]


def test_mapping_and_attribute_rows_group_alike():
    model = _field_model()
    spec = [("name", None)]
    objects = [FieldRow("a", None), FieldRow("b", None), FieldRow("a", None)]
    mappings = [dataclasses.asdict(row) for row in objects]

    assert compute_row_aggregation(
        objects, model=model, group_by=spec, aggregates=COUNT,
    ) == compute_row_aggregation(
        mappings, model=model, group_by=spec, aggregates=COUNT,
    )


# --- fail loud


@pytest.mark.parametrize(("arguments", "error"), [
    ({"aggregates": [(AggregateOp.SUM, "quantity")]},
     OperatorNotSupportedError),
    ({"aggregates": [(AggregateOp.COUNT_DISTINCT, "status")]},
     OperatorNotSupportedError),
    ({"group_by": [("missing", None)]}, GroupByFieldNotAllowed),
    ({"group_by": [("status__name", None)]}, GroupByFieldNotAllowed),
    ({"group_by": [("status", TimeGranularity.MONTH)]},
     GranularityNotApplicable),
    ({"group_by": [("status", None)], "having": {"sum_total__gt": 1}},
     HavingFieldNotAllowed),
    ({"having": {"count__gt": 1}}, AggregateError),
    ({"group_by": [("status", None)], "order_by": [("total", "asc", None)]},
     OrderFieldNotAllowed),
    ({"group_by": [("status", None)], "offset": -1}, ValueError),
    ({"group_by": [("status", None)], "limit": -1}, ValueError),
    ({"week_start": 8}, ValueError),
])
def test_invalid_requests_fail_before_reading_rows(arguments, error):
    def unread():
        raise AssertionError("rows must not be read")
        yield  # pragma: no cover

    arguments = {"aggregates": COUNT, **arguments}
    with pytest.raises(error):
        compute_row_aggregation(
            unread(), model=_order_rows_model(), **arguments,
        )


@pytest.mark.parametrize("granularity", [
    TimeGranularity.HOUR, NumberGranularity.MINUTE_NUMBER,
])
def test_time_of_day_granularity_is_refused_on_a_date_column(granularity):
    with pytest.raises(GranularityNotApplicable):
        compute_row_aggregation(
            [], model=_field_model(),
            group_by=[("created", granularity)], aggregates=COUNT,
        )


def test_a_time_column_takes_no_granularity():
    model = make_row_model("Clock", {"at": datetime.time})

    with pytest.raises(GranularityNotApplicable):
        compute_row_aggregation(
            [], model=model,
            group_by=[("at", TimeGranularity.HOUR)], aggregates=COUNT,
        )


def test_relation_and_json_columns_of_a_concrete_model_are_refused():
    for path in ("customer", "metadata"):
        with pytest.raises(GroupByFieldNotAllowed):
            compute_row_aggregation(
                [], model=Order, group_by=[(path, None)], aggregates=COUNT,
            )


@pytest.mark.parametrize("row", [
    {"name": "a"},
    types.SimpleNamespace(name="a"),
])
def test_a_missing_row_column_fails_loud(row):
    with pytest.raises(AggregateError, match="relation_target"):
        compute_row_aggregation(
            [row], model=_field_model(),
            group_by=[("relation_target", None)], aggregates=COUNT,
        )


@pytest.mark.parametrize(("column", "value"), [
    ("name", 7),
    ("created", datetime.datetime(2026, 5, 1, tzinfo=datetime.UTC)),
])
def test_a_value_of_the_wrong_type_fails_loud(column, value):
    row = {"name": "a", "relation_target": None, "created": None}
    row[column] = value

    with pytest.raises(AggregateError, match=f"`{column}`"):
        compute_row_aggregation(
            [row], model=_field_model(),
            group_by=[(column, None)], aggregates=COUNT,
        )


@pytest.mark.parametrize(("columns", "group_by"), [
    ({"count": int}, [("count", None)]),
    (
        {"created": datetime.datetime, "created_month": datetime.datetime},
        [("created", TimeGranularity.MONTH), ("created_month", None)],
    ),
])
def test_result_keys_that_would_overwrite_each_other_are_refused(
    columns, group_by,
):
    with pytest.raises(GroupByFieldNotAllowed, match="count|created_month"):
        compute_row_aggregation(
            [], model=make_row_model("Colliding", columns),
            group_by=group_by, aggregates=COUNT,
        )


@pytest.mark.parametrize("column", ["Meta", "save", "check"])
def test_row_model_rejects_model_attribute_names(column):
    with pytest.raises(TypeError, match="model attribute"):
        make_row_model("Shadowing", {column: str})


# --- datetimes follow Django's USE_TZ reading -------------------------------


def _when_model():
    return make_row_model("Moments", {"at": datetime.datetime})


def test_naive_datetimes_read_in_the_default_timezone(settings):
    settings.TIME_ZONE = "Asia/Tokyo"
    # 03:00 in Tokyo on May 11 is 18:00 UTC on May 10.
    rows = [{"at": datetime.datetime(2026, 5, 11, 3, 0)}]

    day = compute_row_aggregation(
        rows, model=_when_model(), aggregates=COUNT, tz="UTC",
        group_by=[("at", TimeGranularity.DAY)],
    )[0]["at_day"]
    raw = compute_row_aggregation(
        rows, model=_when_model(), aggregates=COUNT,
        group_by=[("at", None)],
    )[0]["at"]

    assert day.isoformat() == "2026-05-10T00:00:00+00:00"
    assert raw.isoformat() == "2026-05-10T18:00:00+00:00"


def test_without_use_tz_values_keep_their_wall_clock(settings):
    settings.USE_TZ = False
    rows = [{"at": datetime.datetime(2026, 5, 11, 3, 0)}]

    day = compute_row_aggregation(
        rows, model=_when_model(), aggregates=COUNT, tz="Asia/Tokyo",
        group_by=[("at", TimeGranularity.DAY)],
    )[0]["at_day"]

    assert day == datetime.datetime(2026, 5, 11)


def test_without_use_tz_aware_values_are_refused(settings):
    settings.USE_TZ = False
    rows = [{"at": datetime.datetime(2026, 5, 11, 3, tzinfo=datetime.UTC)}]

    with pytest.raises(AggregateError, match="USE_TZ"):
        compute_row_aggregation(
            rows, model=_when_model(), aggregates=COUNT,
            group_by=[("at", None)],
        )


def test_unbucketed_datetimes_group_by_utc_instant():
    tokyo = datetime.datetime(
        2026, 5, 11, 3, 0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=9)),
    )
    utc = datetime.datetime(2026, 5, 10, 18, 0, tzinfo=datetime.UTC)

    for rows in ([{"at": tokyo}, {"at": utc}], [{"at": utc}, {"at": tokyo}]):
        assert compute_row_aggregation(
            rows, model=_when_model(), aggregates=COUNT,
            group_by=[("at", None)],
        ) == [{"at": utc, "count": 2}]


def test_an_ambiguous_local_hour_is_one_bucket_labeled_first():
    # 2026-11-01 01:30 happens twice in New York (EDT, then EST).
    rows = [
        {"at": datetime.datetime(2026, 11, 1, 5, 30, tzinfo=datetime.UTC)},
        {"at": datetime.datetime(2026, 11, 1, 6, 30, tzinfo=datetime.UTC)},
    ]

    for ordered in (rows, rows[::-1]):
        result = compute_row_aggregation(
            ordered, model=_when_model(), aggregates=COUNT,
            tz="America/New_York", group_by=[("at", TimeGranularity.HOUR)],
        )
        assert [
            (row["at_hour"].isoformat(), row["count"]) for row in result
        ] == [("2026-11-01T01:00:00-04:00", 2)]


# --- the shared in-memory orderer --------------------------------------------


@pytest.mark.django_db
def test_null_key_ordering_matches_the_queryset_path(sample_orders):
    Order.objects.filter(status="draft").update(total=None)
    group_by = [("total", None)]

    for direction, nulls in (("asc", "first"), ("desc", "last")):
        order_by = [("total", direction, nulls)]
        assert compute_row_aggregation(
            _order_rows(), model=_order_rows_model(), group_by=group_by,
            aggregates=COUNT, order_by=order_by,
        ) == compute_aggregation(
            Order.objects.all(), group_by=group_by, aggregates=COUNT,
            order_by=order_by,
        )


@pytest.mark.django_db
def test_filled_buckets_keep_null_measures_first_on_desc(sample_orders):
    """Filler rows carry NULL measures; ``desc`` puts them first."""

    def totals(nulls):
        rows = compute_aggregation(
            Order.objects.all(),
            group_by=[("created_at", TimeGranularity.WEEK)],
            aggregates=[*COUNT, (AggregateOp.SUM, "total")],
            fill=True,
            order_by=[("sum_total", "desc", nulls)],
        )
        return [row["sum_total"] for row in rows]

    default, last = totals(None), totals("last")
    filled = default.count(None)

    assert filled > 0
    assert default[:filled] == [None] * filled
    assert last[-filled:] == [None] * filled
    assert default[filled:] == last[:-filled]
    assert default[filled:] == sorted(default[filled:], reverse=True)
