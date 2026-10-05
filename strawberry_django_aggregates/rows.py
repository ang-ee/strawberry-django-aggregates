"""Grouping over computed rows — the in-memory sibling of the compiler.

Some resources are not Django tables: their rows come from a Python
callable (schema introspection, a foreign API, a report). They still want
the same grouping contract as a queryset — the same ``<Model>GroupKey`` /
``<Model>GroupBySpec`` / ``<Model>Having`` / ``<Model>GroupOrder`` types,
the same translators and key shapers, and the same result rows. SPEC § 20.

Two public pieces:

- :func:`make_row_model` declares the row columns as an **abstract** Django
  model (real ``Field`` instances, no table, never registered with the app
  registry). Every type generator and :class:`AggregateBuilder` translator
  already reads field facts from ``model._meta``, so the row model reuses
  them unchanged instead of growing a parallel type emitter.
- :func:`compute_row_aggregation` groups an iterable of rows and returns
  the same flat ``list[dict]`` as :func:`compiler.compute_aggregation`, so
  ``AggregateBuilder.shape_group_key`` and ``shape_aggregate_row`` shape
  it unchanged. HAVING parsing, order validation, ordering, null placement
  and TIME bucket truncation reuse the compiler / fill implementations.

CLAUDE.md Critical Rule 9 — framework-agnostic: Django is allowed (field
facts, settings), Strawberry is not.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import keyword
import operator
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import models
from django.utils import timezone

from strawberry_django_aggregates.aliasing import group_by_alias
from strawberry_django_aggregates.compiler import (
    HAVING_COMPARISONS,
    _apply_order_to_rows,
    aggregate_alias,
    parse_having,
    resolve_field_to_one_only,
    validate_order_aliases,
)
from strawberry_django_aggregates.errors import (
    AggregateError,
    GranularityNotApplicable,
    GroupByFieldNotAllowed,
    OperatorNotSupportedError,
)
from strawberry_django_aggregates.fill import _floor
from strawberry_django_aggregates.granularity import (
    Granularity,
    NumberGranularity,
    TimeGranularity,
    validate_week_start,
)
from strawberry_django_aggregates.operators import AggregateOp

if TYPE_CHECKING:
    from django.db.models import Field, Model


# ---------------------------------------------------------------------------
# Row model — column declarations as an abstract Django model
# ---------------------------------------------------------------------------

# Class attribute marking a ``make_row_model`` class. Uses the library's
# reserved ``_sda_`` prefix; read it through :func:`is_row_model`.
_ROW_MODEL_MARKER = "_sda_row_model"

# Python column type -> Django field class, matched by exact type so the
# ``bool``/``int`` and ``datetime``/``date`` subclass pairs never shadow
# each other.
_COLUMN_FIELDS: dict[type, type[Field]] = {
    bool:              models.BooleanField,
    int:               models.IntegerField,
    float:             models.FloatField,
    decimal.Decimal:   models.DecimalField,
    str:               models.CharField,
    datetime.datetime: models.DateTimeField,
    datetime.date:     models.DateField,
    datetime.time:     models.TimeField,
    uuid.UUID:         models.UUIDField,
}

# Time-of-day granularities have no meaning on a calendar date. The SQL
# path refuses them on a ``DateField`` too (Django cannot truncate or
# extract a time component from a date column).
_TIME_OF_DAY: frozenset[Granularity] = frozenset({
    TimeGranularity.HOUR,
    TimeGranularity.MINUTE,
    TimeGranularity.SECOND,
    NumberGranularity.HOUR_NUMBER,
    NumberGranularity.MINUTE_NUMBER,
    NumberGranularity.SECOND_NUMBER,
})


def is_row_model(model: Any) -> bool:
    """Whether ``model`` was declared by :func:`make_row_model`."""
    return bool(getattr(model, _ROW_MODEL_MARKER, False))


def _validate_column_name(model_name: str, column: str) -> None:
    if (
        not isinstance(column, str)
        or not column.isidentifier()
        or keyword.iskeyword(column)
        or column.startswith("_")
        or column.endswith("_")
        or "__" in column
        or column == "Meta"
        or hasattr(models.Model, column)
    ):
        raise TypeError(
            f"make_row_model({model_name!r}) column {column!r} must be a "
            "Python identifier without leading/trailing underscores or "
            "'__', and must not shadow a Django model attribute (such as "
            "'pk', 'save' or 'Meta')."
        )


def _enum_column_field(
    model_name: str, column: str, enum_cls: type[enum.Enum],
) -> Field:
    """A choices field whose generated group-key enum mirrors ``enum_cls``.

    ``choices_enum`` is the duck-typed attribute ``types._choices_enum_for``
    already reads (SPEC § 4.3): member names and values are reused verbatim,
    so the group key serializes the same member names as the Python enum.
    """
    members = list(enum_cls)
    values = [member.value for member in members]
    field_cls: type[Field]
    if members and all(isinstance(value, str) for value in values):
        field_cls = models.CharField
    elif members and all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in values
    ):
        field_cls = models.IntegerField
    else:
        raise TypeError(
            f"make_row_model({model_name!r}) column {column!r}: enum "
            f"{enum_cls.__name__} needs members with all-str or all-int "
            "values."
        )
    field = field_cls(
        choices=[(member.value, member.name) for member in members],
        null=True,
    )
    field.choices_enum = enum_cls  # type: ignore[union-attr]
    return field


def _column_field(model_name: str, column: str, python_type: Any) -> Field:
    if isinstance(python_type, type) and issubclass(python_type, enum.Enum):
        return _enum_column_field(model_name, column, python_type)
    field_cls = _COLUMN_FIELDS.get(python_type)
    if field_cls is None:
        supported = sorted(t.__name__ for t in _COLUMN_FIELDS)
        raise TypeError(
            f"make_row_model({model_name!r}) column {column!r} has "
            f"unsupported type {python_type!r}; expected one of "
            f"{supported} or an Enum subclass."
        )
    return field_cls(null=True)


def make_row_model(
    name: str, columns: Mapping[str, Any],
) -> type[Model]:
    """Declare computed-row columns as an abstract Django model.

    ``columns`` maps each column name (the row attribute or mapping key) to
    its Python type: ``bool``, ``int``, ``float``, ``Decimal``, ``str``,
    ``datetime``, ``date``, ``time``, ``UUID``, or an ``Enum`` subclass
    with all-str or all-int values. Every column is nullable. An enum
    column becomes a choices field whose group key reuses the enum's member
    names and values (SPEC § 4.3).

    The result is an abstract model: it has no table, no manager and no
    primary key, and Django never registers it with the app registry. Pass
    it as ``model=`` to :class:`AggregateBuilder` for the grouping types,
    translators and key shapers, and to :func:`compute_row_aggregation` for
    execution. ``name`` is the class name and the builder's default type
    prefix. Requires a ready app registry (call after ``django.setup()``).
    Build each row model once: generated enum types are cached per type
    prefix and column, so the first vocabulary wins (SPEC § 4.3).
    """
    if not isinstance(name, str) or not name.isidentifier():
        raise TypeError(
            f"make_row_model name {name!r} must be a Python identifier."
        )
    attrs: dict[str, Any] = {
        "__module__": __name__,
        "Meta": type("Meta", (), {"abstract": True}),
        _ROW_MODEL_MARKER: True,
    }
    for column, python_type in columns.items():
        _validate_column_name(name, column)
        attrs[column] = _column_field(name, column, python_type)
    return cast("type[Model]", type(name, (models.Model,), attrs))


# ---------------------------------------------------------------------------
# Execution — group rows in Python with the compiler's semantics
# ---------------------------------------------------------------------------

# Python twins of ``compiler._HAVING_LOOKUP``. A NULL measure fails every
# comparison, as SQL three-valued logic excludes it from HAVING.
_HAVING_PREDICATES: dict[str, Callable[[Any, Any], bool]] = {
    "gt":     operator.gt,
    "lt":     operator.lt,
    "lte":    operator.le,
    "gte":    operator.ge,
    "eq":     operator.eq,
    "neq":    operator.ne,
    "in":     lambda value, operand: value in operand,
    "not_in": lambda value, operand: value not in operand,
}

# Fail at import if the HAVING vocabulary grows without a Python twin.
if set(_HAVING_PREDICATES) != set(HAVING_COMPARISONS):
    raise RuntimeError(
        "rows._HAVING_PREDICATES must mirror compiler.HAVING_COMPARISONS; "
        f"differs by {set(_HAVING_PREDICATES) ^ set(HAVING_COMPARISONS)}"
    )

# Operators the in-memory path computes. Every other operator raises
# ``OperatorNotSupportedError`` before any row is read.
_ROW_OPERATORS: frozenset[AggregateOp] = frozenset({AggregateOp.COUNT})

# Python ``date_part(...)::int`` for the NUMBER track (SPEC § 7), keyed like
# ``compiler._NUMBER_LOOKUP``. Each part reads a datetime and ``week_start``.
_DATE_PARTS: dict[
    NumberGranularity, Callable[[datetime.datetime, int], int],
] = {
    NumberGranularity.YEAR_NUMBER:     lambda dt, _ws: dt.year,
    NumberGranularity.QUARTER_NUMBER:  lambda dt, _ws: (dt.month - 1) // 3 + 1,
    NumberGranularity.MONTH_NUMBER:    lambda dt, _ws: dt.month,
    NumberGranularity.ISO_WEEK_NUMBER: lambda dt, _ws: dt.isocalendar().week,
    NumberGranularity.DAY_OF_YEAR:     lambda dt, _ws: dt.timetuple().tm_yday,
    NumberGranularity.DAY_OF_MONTH:    lambda dt, _ws: dt.day,
    # Rotated like ``compiler._extract_day_of_week_rotated``: the caller's
    # ``week_start`` is day 1.
    NumberGranularity.DAY_OF_WEEK: (
        lambda dt, ws: (dt.isoweekday() - ws) % 7 + 1
    ),
    NumberGranularity.HOUR_NUMBER:     lambda dt, _ws: dt.hour,
    NumberGranularity.MINUTE_NUMBER:   lambda dt, _ws: dt.minute,
    NumberGranularity.SECOND_NUMBER:   lambda dt, _ws: dt.second,
}

if set(_DATE_PARTS) != set(NumberGranularity):
    raise RuntimeError("rows._DATE_PARTS must cover every NumberGranularity")


def _is_number(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# Declared field class -> (accepted-value check, expected type name), most
# specific class first (``DateTimeField`` subclasses ``DateField``). Columns
# of other field classes are not type-checked.
_VALUE_CHECKS: tuple[tuple[type[Field], Callable[[Any], bool], str], ...] = (
    (models.BooleanField, lambda v: isinstance(v, bool), "bool"),
    (
        models.DateTimeField,
        lambda v: isinstance(v, datetime.datetime),
        "datetime",
    ),
    (
        models.DateField,
        lambda v: isinstance(v, datetime.date)
        and not isinstance(v, datetime.datetime),
        "date",
    ),
    (models.TimeField, lambda v: isinstance(v, datetime.time), "time"),
    (
        models.DecimalField,
        lambda v: isinstance(v, decimal.Decimal) or _is_number(v),
        "Decimal",
    ),
    (
        models.FloatField,
        lambda v: isinstance(v, float) or _is_number(v),
        "float",
    ),
    (models.IntegerField, _is_number, "int"),
    (models.UUIDField, lambda v: isinstance(v, uuid.UUID), "UUID"),
    (models.CharField, lambda v: isinstance(v, str), "str"),
    (models.TextField, lambda v: isinstance(v, str), "str"),
)


@dataclass(frozen=True)
class _Axis:
    """One validated group-by axis of a row aggregation."""

    path: str
    attribute: str
    alias: str
    granularity: Granularity | None
    field: Any
    check: Callable[[Any], bool] | None
    expected: str


def _resolve_row_axis(
    model: type[Model], field_path: str, granularity: Granularity | None,
) -> _Axis:
    """Validate one row axis against the model's declared columns.

    Rows group by direct scalar columns only: relation paths, relation
    fields and JSON columns are refused, and granularity applies only to
    date / datetime columns (never time-of-day parts on a date) — the
    columns whose group key carries bucket fields.
    """
    if "__" in field_path:
        raise GroupByFieldNotAllowed(
            f"Row aggregation groups by direct columns; `{field_path}` "
            f"traverses a relation on `{model.__name__}`."
        )
    field = resolve_field_to_one_only(
        model, field_path, GroupByFieldNotAllowed,
    )
    if getattr(field, "is_relation", False) or isinstance(
        field, models.JSONField,
    ):
        raise GroupByFieldNotAllowed(
            f"Row aggregation groups by scalar columns; `{field_path}` on "
            f"`{model.__name__}` is a {type(field).__name__}."
        )
    if granularity is not None:
        is_date = isinstance(field, models.DateField)
        is_datetime = isinstance(field, models.DateTimeField)
        if not is_date or (
            not is_datetime and granularity in _TIME_OF_DAY
        ):
            raise GranularityNotApplicable(
                f"Granularity {granularity!r} cannot apply to field "
                f"`{field_path}` of type {type(field).__name__}."
            )
    check, expected = next(
        (
            (accepts, name)
            for field_cls, accepts, name in _VALUE_CHECKS
            if isinstance(field, field_cls)
        ),
        (None, ""),
    )
    return _Axis(
        path=field_path,
        attribute=field.attname,
        alias=group_by_alias(field_path, granularity, field),
        granularity=granularity,
        field=field,
        check=check,
        expected=expected,
    )


def _validate_aliases(
    axes: list[_Axis], measure_aliases: list[str],
) -> None:
    """Refuse result keys that would overwrite each other.

    The SQL path refuses the same collisions (Django rejects an annotation
    that conflicts with a selected column).
    """
    owners: dict[str, tuple[str, Granularity | None]] = {}
    for axis in axes:
        owner = owners.setdefault(axis.alias, (axis.path, axis.granularity))
        if owner != (axis.path, axis.granularity):
            raise GroupByFieldNotAllowed(
                f"Group-by axes `{owner[0]}` and `{axis.path}` both "
                f"produce the result key `{axis.alias}`."
            )
    if clash := sorted(set(owners) & set(measure_aliases)):
        raise GroupByFieldNotAllowed(
            f"Group-by result keys {clash} collide with aggregate aliases."
        )


def _row_value(row: Any, axis: _Axis, model: type[Model]) -> Any:
    """Read and type-check one column from a mapping or attribute row.

    An enum member normalizes to its stored ``value`` — the same raw value
    ``.values()`` returns for a choices column, which the builder's key
    shaping coerces to the generated group-key enum.
    """
    try:
        value = (
            row[axis.attribute]
            if isinstance(row, Mapping)
            else getattr(row, axis.attribute)
        )
    except (KeyError, AttributeError) as exc:
        raise AggregateError(
            f"Row {type(row).__name__} has no column `{axis.attribute}` "
            f"declared by `{model.__name__}`."
        ) from exc
    if isinstance(value, enum.Enum):
        value = value.value
    if value is not None and axis.check is not None and not axis.check(value):
        raise AggregateError(
            f"Column `{axis.path}` of `{model.__name__}` is declared "
            f"{axis.expected} but a row holds {type(value).__name__} "
            f"{value!r}."
        )
    return value


def _datetime_key(
    value: datetime.datetime,
    granularity: Granularity | None,
    tzinfo: ZoneInfo,
    week_start: int,
) -> Any:
    """Key a datetime the way Django reads and buckets a DateTimeField.

    With ``USE_TZ`` a naive value is read in the default timezone. An
    unbucketed key is the UTC instant (what the database returns); a
    bucketed one converts to ``tzinfo`` BEFORE truncating (Critical Rule
    5). Without ``USE_TZ`` values keep their own wall clock. TIME buckets
    reuse ``fill._floor`` — the Python ``date_trunc`` of the dense-fill
    spine — and resolve an ambiguous local time to its first occurrence.
    """
    if settings.USE_TZ:
        if timezone.is_naive(value):
            value = timezone.make_aware(value, timezone.get_default_timezone())
        value = value.astimezone(
            datetime.UTC if granularity is None else tzinfo,
        )
    if granularity is None:
        return value
    if isinstance(granularity, TimeGranularity):
        return _floor(value, granularity, week_start).replace(fold=0)
    return _DATE_PARTS[granularity](value, week_start)


def _date_key(
    value: datetime.date, granularity: Granularity | None, week_start: int,
) -> Any:
    """Key a date: TIME buckets stay dates, as ``Trunc`` on a DateField."""
    if granularity is None:
        return value
    midnight = datetime.datetime.combine(value, datetime.time())
    if isinstance(granularity, TimeGranularity):
        return _floor(midnight, granularity, week_start).date()
    return _DATE_PARTS[granularity](midnight, week_start)


def _axis_key(
    value: Any, axis: _Axis, tzinfo: ZoneInfo, week_start: int,
) -> Any:
    if value is None:
        return None
    if isinstance(axis.field, models.DateTimeField):
        return _datetime_key(value, axis.granularity, tzinfo, week_start)
    if isinstance(axis.field, models.DateField):
        return _date_key(value, axis.granularity, week_start)
    return value


def _validate_page(offset: int, limit: int | None) -> None:
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError(
            "Row aggregation offset and limit must be non-negative; got "
            f"offset={offset!r}, limit={limit!r}."
        )


def _validate_row_aggregates(
    aggregates: list[tuple[AggregateOp, str | None]],
) -> None:
    for op, field_path in aggregates:
        if op not in _ROW_OPERATORS:
            raise OperatorNotSupportedError(
                f"Aggregate operator {op.value!r} is not supported by "
                "in-memory row aggregation, which computes `count` only. "
                "Aggregate a queryset with compute_aggregation instead."
            )
        if field_path is not None:
            raise OperatorNotSupportedError(
                "In-memory row aggregation counts rows; `count` takes no "
                f"field path (got {field_path!r})."
            )


def compute_row_aggregation(
    rows: Iterable[Any],
    *,
    model: type[Model],
    group_by:   list[tuple[str, Granularity | None]] | None = None,
    aggregates: list[tuple[AggregateOp, str | None]]   | None = None,
    having:     dict[str, Any]                          | None = None,
    order_by:   list[tuple[str, str, str | None]]      | None = None,
    offset:     int = 0,
    limit:      int | None = None,
    tz:         str | None = None,
    week_start: int = 1,
) -> list[dict[str, Any]]:
    """Group computed rows with :func:`compute_aggregation` semantics.

    ``rows`` is an iterable of mappings or attribute objects (dataclasses,
    pydantic models, model instances); ``model`` declares their columns —
    usually :func:`make_row_model`. The arguments mirror
    :func:`compute_aggregation` and the result is the same flat row list:
    one dict per group, holding the canonical group aliases plus the
    requested aggregate aliases, so the builder's shapers apply unchanged.
    Rows are permission-naive input: scope them before calling (Rule 1).

    Semantics shared with the SQL path (SPEC § 20):

    - **Keys.** NULL and ``""`` are distinct buckets. Enum members group
      by their stored value. Each value must match its column's declared
      type, and every row must have every grouped column. Datetimes follow
      Django's ``USE_TZ`` reading; date axes convert into ``tz`` (default
      ``settings.TIME_ZONE``) before truncating, and TIME buckets and
      NUMBER parts match ``Trunc`` / ``Extract``, including ``week_start``.
    - **Measures.** ``COUNT`` only; any other operator raises
      :class:`OperatorNotSupportedError` before rows are read.
    - **HAVING / ordering.** HAVING keys and order terms are validated by
      the compiler's own parsers (unknown names fail loud), and result keys
      that would overwrite each other are refused. Groups sort by their key
      tuple ascending (NULLs last) for a deterministic default, then
      ``order_by`` applies with the dense-fill path's in-memory orderer:
      ``asc`` puts NULLs last, ``desc`` first, unless ``nulls`` says
      otherwise. Strings sort by Python code point, not a collation.
    - **Paging.** ``offset`` / ``limit`` slice the ordered groups. Without
      ``group_by`` the result is one row and paging is ignored, exactly
      like ``compute_aggregation``.

    The exact group cardinality is ``len()`` of the unpaged result.
    """
    group_by = group_by or []
    aggregates = aggregates or []
    having = having or {}
    order_by = order_by or []
    week_start = validate_week_start(week_start)
    tzinfo = ZoneInfo(tz or settings.TIME_ZONE)
    _validate_page(offset, limit)
    if having and not group_by:
        raise AggregateError(
            "HAVING requires a non-empty `group_by` — there is nothing "
            "to filter without group buckets. Add a `group_by` or "
            "filter the rows before aggregating."
        )
    _validate_row_aggregates(aggregates)
    axes = [
        _resolve_row_axis(model, field_path, granularity)
        for field_path, granularity in group_by
    ]
    group_aliases = [axis.alias for axis in axes]
    measure_aliases = [aggregate_alias(op, fp) for op, fp in aggregates]
    _validate_aliases(axes, measure_aliases)
    having_terms = parse_having(having, measure_aliases)
    validate_order_aliases(order_by, group_aliases, measure_aliases)

    materialized = list(rows)
    if not axes:
        return [{alias: len(materialized) for alias in measure_aliases}]

    counts: dict[tuple[Any, ...], int] = {}
    for row in materialized:
        key = tuple(
            _axis_key(_row_value(row, axis, model), axis, tzinfo, week_start)
            for axis in axes
        )
        counts[key] = counts.get(key, 0) + 1

    result: list[dict[str, Any]] = []
    for key, count in counts.items():
        result_row = dict(zip(group_aliases, key, strict=True))
        result_row.update({alias: count for alias in measure_aliases})
        if all(
            result_row[alias] is not None
            and _HAVING_PREDICATES[comparison](result_row[alias], value)
            for alias, comparison, value in having_terms
        ):
            result.append(result_row)

    result = _apply_order_to_rows(
        result, [(alias, "asc", None) for alias in group_aliases],
    )
    result = _apply_order_to_rows(result, order_by)
    stop = offset + limit if limit is not None else None
    return result[offset:stop]
