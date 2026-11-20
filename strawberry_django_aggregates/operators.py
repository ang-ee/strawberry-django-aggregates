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
    "db_type", "from_db_value", "get_db_converters", "to_python",
)
"""How a field stores its value and turns it back into a Python value."""


class FieldKind(NamedTuple):
    """What a field is to this library (SPEC § 5): one answer for both sides.

    ``stored`` is the temporal or numeric Django field type the field is an
    instance of, ``None`` when it is none: the compiler reads it to decide
    what SQL the column admits (date truncation, the time zone wrap, an
    integer sum widened to ``bigint``). ``name`` is the type the field
    aggregates as: the schema reads it for the GraphQL type, the date
    buckets, the SUM and AVG types and the default operators. ``name`` is
    ``stored`` only where the field returns the stored type's values
    (:func:`field_kind`); otherwise it is the field's class name.
    """

    stored: str | None
    name: str


def field_kind(field: object) -> FieldKind:
    """Return what ``field`` is, for the schema and the compiler alike.

    A Django temporal or numeric field (``DateTimeField``, ``DateField``,
    ``TimeField``, ``DecimalField``, ``FloatField``, the integer types) is
    that type. A subclass of one aggregates as that type when it stores and
    returns the same values: its class is not one of Django's own
    (``OrderWrt``, the auto primary keys), its ``get_internal_type()``
    names that type, and it overrides none of ``db_type``,
    ``from_db_value``, ``get_db_converters`` and ``to_python`` relative to
    that type. A created-time field on ``DateTimeField`` or a money field on
    ``DecimalField`` is such a subclass. A subclass that returns other
    values (an enum member, an opaque token) keeps its class name: no
    inherited type, buckets or default operators; it opts in through
    ``operators=``. Any other field is its class name: a string or duration
    field and its subclasses, a ``GeneratedField`` of any output, a
    relation and a reverse relation.
    """
    cls = type(field)
    base = next((t for t in _STORED_TYPES if isinstance(field, t)), None)
    if base is None:
        return FieldKind(None, cls.__name__)
    stored = base.__name__
    internal = getattr(field, "get_internal_type", None)
    inherits = cls is base or (
        cls.__module__.partition(".")[0] != "django"
        and internal is not None
        and internal() == stored
        and all(
            getattr(cls, hook, None) is getattr(base, hook, None)
            for hook in _VALUE_HOOKS
        )
    )
    return FieldKind(stored, stored if inherits else cls.__name__)


def field_type_for(field: object) -> str:
    """Return the type ``field`` aggregates as: :func:`field_kind`'s name."""
    return field_kind(field).name


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
