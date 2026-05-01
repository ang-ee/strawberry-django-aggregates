"""Type generators for strawberry-django-aggregates.

Each function takes a Django model + field allowlists and returns a
strawberry type / input. Generation is deterministic for a given input.

Public surface:

- :func:`make_aggregate_type` — emits ``<Model>Aggregate`` and the
  per-operator ``<Model>SumFields``/``<Model>AvgFields``/etc. nested
  types.
- :func:`make_grouped_type` — emits ``<Model>GroupKey``,
  ``<Model>Grouped``, and ``<Model>GroupedResult``.
- :func:`make_having_input` — emits ``<Model>Having``.
- :func:`make_group_by_spec` — emits ``<Model>GroupBySpec`` plus the
  groupable-field enum.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Model

from strawberry_django_aggregates.operators import AggregateOp


def make_aggregate_type(
    model: type[Model],
    *,
    name: str | None = None,
    aggregate_fields: list[str] | None = None,
    operators: dict[str, tuple[AggregateOp, ...]] | None = None,
) -> type:
    """Build the ``<Model>Aggregate`` strawberry type.

    Parameters
    ----------
    model : Django model
    name : explicit type name; defaults to ``f"{model.__name__}Aggregate"``
    aggregate_fields : fields eligible for sum/avg/min/max/etc. If
        ``None``, all numeric/date/boolean fields on the model are
        considered.
    operators : per-field operator overrides. Keys are field names;
        values are tuples of permitted :class:`AggregateOp`. Fields
        absent from this dict get the type-derived defaults from
        :func:`operators.default_operators_for`.

    Returns
    -------
    type
        A strawberry type decorated class. Use as a return annotation
        on a strawberry field.
    """
    raise NotImplementedError("Implementation pending — see docs/SPEC.md")


def make_grouped_type(
    model: type[Model],
    *,
    name: str | None = None,
    aggregate_type: type | None = None,
    group_by_fields: list[str] | None = None,
) -> tuple[type, type, type]:
    """Build ``<Model>GroupKey``, ``<Model>Grouped``, and
    ``<Model>GroupedResult`` types.

    Returns the tuple ``(group_key_type, grouped_type, grouped_result_type)``.

    The grouped type is FLAT — it has no ``subgroups`` recursion. Multi-
    level group-by produces multiple result rows with composite keys.
    """
    raise NotImplementedError("Implementation pending — see docs/SPEC.md")


def make_having_input(
    model: type[Model],
    *,
    name: str | None = None,
    aggregate_fields: list[str] | None = None,
    operators: dict[str, tuple[AggregateOp, ...]] | None = None,
) -> type:
    """Build the ``<Model>Having`` strawberry input type.

    Emits one input field per ``(measure, comparison)`` pair where:

    - ``measure`` is ``count``, ``count_distinct``, or ``<op>_<field>``
      for each ``(field, op)`` in the allowlist.
    - ``comparison`` is ``Gt``, ``Lt``, ``Gte``, ``Lte``, ``Eq``,
      ``Neq``, ``In``, ``NotIn``.
    """
    raise NotImplementedError("Implementation pending — see docs/SPEC.md")


def make_group_by_spec(
    model: type[Model],
    *,
    name: str | None = None,
    group_by_fields: list[str] | None = None,
) -> tuple[type, type]:
    """Build ``<Model>GroupBySpec`` (input) and ``<Model>GroupableField``
    (enum). Returns ``(spec_type, enum_type)``.

    The spec input has fields ``field: <Model>GroupableField!`` and
    ``granularity: Granularity`` (the union of TimeGranularity |
    NumberGranularity, nullable; required only on date/datetime fields).
    """
    raise NotImplementedError("Implementation pending — see docs/SPEC.md")


def make_group_order_input(
    model: type[Model],
    *,
    name: str | None = None,
) -> type:
    """Build ``<Model>GroupOrder`` — order input for groupBy results.

    Accepts ``field: String!`` (a field path or aggregate alias),
    ``direction: OrderDirection!``, ``nulls: NullsPosition``.
    """
    raise NotImplementedError("Implementation pending — see docs/SPEC.md")
