"""Aggregate operator vocabulary.

Mirrors Odoo's ``READ_GROUP_AGGREGATE`` whitelist (``odoo/models.py:196``)
extended with ``stddev``, ``variance``, and ``string_agg`` — additions from
PostGraphile's ``pg-aggregates``. No operator outside this enum is
accepted; consumers cannot pass arbitrary SQL fragments.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, NamedTuple

from django.db import models


class AggregateOp(StrEnum):
    """Whitelisted aggregate operators.

    Database support flags are documented in ``docs/SPEC.md``. The
    Postgres-only operators (``ARRAY_AGG``, ``STRING_AGG``, ``STDDEV``,
    ``VARIANCE``, ``STDDEV_POP``, ``VAR_POP``, ``PERCENTILE_CONT``,
    ``PERCENTILE_DISC``, ``MODE``) raise
    :class:`OperatorNotSupportedError` at resolver entry on SQLite
    connections.
    """

    COUNT                = "count"
    COUNT_DISTINCT       = "count_distinct"
    COUNT_DISTINCT_TUPLE = "count_distinct_tuple"
    SUM                  = "sum"
    AVG                  = "avg"
    MIN                  = "min"
    MAX                  = "max"
    STDDEV               = "stddev"           # PG only — sample stddev
    VARIANCE             = "variance"         # PG only — sample variance
    STDDEV_POP           = "stddev_pop"       # PG only — population stddev
    VAR_POP              = "var_pop"          # PG only — population var
    PERCENTILE_CONT      = "percentile_cont"  # PG only — interpolated pct
    PERCENTILE_DISC      = "percentile_disc"  # PG only — discrete pct
    MODE                 = "mode"             # PG only — most-frequent value
    BOOL_AND             = "bool_and"
    BOOL_OR              = "bool_or"
    ARRAY_AGG            = "array_agg"        # Postgres only
    STRING_AGG           = "string_agg"       # Postgres only


# Operators each Django field type gets without explicit override.
# Consumers can narrow further via the AggregateBuilder allowlist arg.
_NUMERIC_OPS = (
    AggregateOp.SUM,
    AggregateOp.AVG,
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.STDDEV,
    AggregateOp.VARIANCE,
    AggregateOp.STDDEV_POP,
    AggregateOp.VAR_POP,
    AggregateOp.PERCENTILE_CONT,
    AggregateOp.PERCENTILE_DISC,
    AggregateOp.MODE,
)

_DATE_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.PERCENTILE_DISC,
    AggregateOp.MODE,
)

_BOOL_OPS = (
    AggregateOp.BOOL_AND,
    AggregateOp.BOOL_OR,
)

_STRING_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.MODE,
    AggregateOp.ARRAY_AGG,
    AggregateOp.STRING_AGG,
)

_ID_OPS = (
    AggregateOp.ARRAY_AGG,
)


# Default operator allowlists for declared JSON-path types. Mirrors the
# field-type defaults above, but keyed on the wire-level type tokens the
# caller passes in :class:`AggregateBuilder.json_paths`. Per SPEC § 6.1.
#
# Numeric JSON paths get the same operator surface as native numeric
# Field types (sum/avg/min/max/stddev/variance/stddev_pop/var_pop) —
# ``Cast(KeyTextTransform(...))`` widens cleanly through the SQL
# variance/stddev aggregates on Postgres. ``MODE`` is excluded across
# all JSON-path types in v1.0 — ordered-set aggregates over the JSON
# cast wrap (``MODE() WITHIN GROUP (ORDER BY ...)``) are not validated
# against the casted expression yet; revisit in v1.x.
_JSON_NUMERIC_OPS = (
    AggregateOp.SUM,
    AggregateOp.AVG,
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.STDDEV,
    AggregateOp.VARIANCE,
    AggregateOp.STDDEV_POP,
    AggregateOp.VAR_POP,
)

_JSON_STRING_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
    AggregateOp.ARRAY_AGG,
    AggregateOp.STRING_AGG,
)

_JSON_BOOL_OPS = (
    AggregateOp.BOOL_AND,
    AggregateOp.BOOL_OR,
)

_JSON_DATE_OPS = (
    AggregateOp.MIN,
    AggregateOp.MAX,
)


def default_operators_for_json_type(
    declared_type: str,
) -> tuple[AggregateOp, ...]:
    """Return default operator allowlist for a declared JSON-path type.

    ``declared_type`` is one of the SPEC § 6.1 wire tokens
    (``"str"``, ``"int"``, ``"float"``, ``"Decimal"``, ``"bool"``,
    ``"date"``, ``"datetime"``). Unknown tokens return an empty tuple.
    """
    if declared_type in {"int", "float", "Decimal"}:
        return _JSON_NUMERIC_OPS
    if declared_type == "str":
        return _JSON_STRING_OPS
    if declared_type == "bool":
        return _JSON_BOOL_OPS
    if declared_type in {"date", "datetime"}:
        return _JSON_DATE_OPS
    return ()


_STRING_TYPES: frozenset[str] = frozenset({
    "CharField", "TextField", "EmailField", "URLField", "SlugField",
})
"""The string field types; each has the string default operators."""

_STORED_TYPES: tuple[type[models.Field[Any, Any]], ...] = (
    models.DateTimeField, models.DateField, models.TimeField,
    models.DecimalField, models.FloatField,
    models.PositiveBigIntegerField, models.PositiveSmallIntegerField,
    models.PositiveIntegerField, models.SmallIntegerField,
    models.BigIntegerField, models.IntegerField,
)
"""The temporal and numeric Django field types a subclass may aggregate as.

Most specific first, so the first a field is an instance of is the type
its column holds. ``DurationField`` is not among them: it has no GraphQL
scalar here (SPEC § 5).
"""

_VALUE_HOOKS: tuple[str, ...] = (
    "db_type", "db_parameters", "from_db_value", "get_db_converters",
    "select_format", "to_python",
)
"""How a field stores, selects and converts its value.

A subclass that overrides any of them, on its class or its instance, may
return other values than its base field and keeps its class name.
"""


class _FieldKind(NamedTuple):
    """What a field is to this library: one answer for both sides (SPEC § 5).

    ``stored`` is the temporal or numeric Django field type the field is an
    instance of, ``None`` when it is none: the compiler reads it to decide
    what SQL the column admits (date truncation, the time zone wrap, an
    integer sum widened to ``bigint``). ``name`` is the type the field
    aggregates as, which :func:`field_type_for` returns and every schema
    decision reads: the GraphQL type, the date buckets, the SUM and AVG
    types and the default operators. ``name`` is ``stored`` only where the
    field returns the stored type's values (:func:`_field_kind`); otherwise
    it is the field's class name.
    """

    stored: str | None
    name: str


def _field_kind(field: object) -> _FieldKind:
    """Return what ``field`` is, for the schema and the compiler alike.

    The internal owner behind :func:`field_type_for`. A Django temporal or
    numeric field (``DateTimeField``, ``DateField``, ``TimeField``,
    ``DecimalField``, ``FloatField``, the integer types) is that type. A
    subclass of one aggregates as that type only when its class is not one
    of Django's own (``OrderWrt``, the auto primary keys), its
    ``get_internal_type()`` names that type, and it overrides none of
    ``db_type``, ``db_parameters``, ``from_db_value``,
    ``get_db_converters``, ``select_format`` and ``to_python`` relative to
    that type, on its class or its instance: a created-time field on
    ``DateTimeField``, an amount field on ``DecimalField``. A subclass that
    overrides any of them keeps its class name, with no inherited type,
    buckets or default operators, and opts in through ``operators=``: one
    that returns an enum member or an opaque token, and also one that
    overrides only ``to_python`` (django-money's ``MoneyField`` among them),
    though Django's read path does not call it: the rule is deliberately
    conservative. Any other field is its class name: a string, UUID,
    boolean, duration, relation or id field and its subclasses, a
    ``GeneratedField`` of any output, a reverse relation.
    """
    cls = type(field)
    base = next((t for t in _STORED_TYPES if isinstance(field, t)), None)
    if base is None:
        return _FieldKind(None, cls.__name__)
    stored = base.__name__
    internal = getattr(field, "get_internal_type", None)
    own = vars(field) if hasattr(field, "__dict__") else {}
    inherits = (
        (cls is base or cls.__module__.partition(".")[0] != "django")
        and internal is not None
        and internal() == stored
        and not any(hook in own for hook in _VALUE_HOOKS)
        and all(
            getattr(cls, hook, None) is getattr(base, hook, None)
            for hook in _VALUE_HOOKS
        )
    )
    return _FieldKind(stored, stored if inherits else cls.__name__)


def field_type_for(field: object) -> str:
    """Return the Django field type ``field`` aggregates as (SPEC § 5).

    The type its default operators, GraphQL types and date buckets follow:
    its temporal or numeric base type for a subclass that stores and returns
    that type's values, else its class name.
    """
    return _field_kind(field).name


def default_operators_for(field_type: str) -> tuple[AggregateOp, ...]:
    """Return the default operator allowlist for a Django field type name.

    ``field_type`` is the type a field aggregates as, as a string (e.g.
    ``"IntegerField"``; :func:`field_type_for` answers it for a field).
    Used by :class:`AggregateBuilder` when the consumer doesn't specify
    per-field overrides.

    Unknown field types return an empty tuple — explicit allowlisting
    required.
    """
    if field_type in {
        "IntegerField", "BigIntegerField", "PositiveIntegerField",
        "PositiveSmallIntegerField", "SmallIntegerField",
        "FloatField", "DecimalField", "DurationField",
    }:
        return _NUMERIC_OPS
    if field_type in {"DateField", "DateTimeField", "TimeField"}:
        return _DATE_OPS
    if field_type == "BooleanField":
        return _BOOL_OPS
    if field_type in _STRING_TYPES:
        return _STRING_OPS
    if field_type in {"UUIDField", "AutoField", "BigAutoField"}:
        return _ID_OPS
    if field_type in {"ForeignKey", "OneToOneField"}:
        return _ID_OPS  # by ID
    return ()
