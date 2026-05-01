"""High-level convenience builder.

Most consumers will reach for :class:`AggregateBuilder` rather than
calling the lower-level type generators directly. The builder bundles:

- All four type generators (``make_aggregate_type``,
  ``make_grouped_type``, ``make_having_input``, ``make_group_by_spec``)
- Two strawberry fields (``aggregate_field`` and ``group_by_field``)
  ready to attach to a ``Query`` type
- Optional integration with strawberry-django filter / order inputs

Lower-level type generators remain available for consumers who need
finer control (see :mod:`strawberry_django_aggregates.types`).
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from django.db.models import Model

from strawberry_django_aggregates.operators import AggregateOp


@dataclass
class AggregateBuilder:
    """Convenience builder — emits all aggregate types and resolver fields
    for a given model.

    Parameters
    ----------
    model : Django model class
    aggregate_fields : fields eligible for sum/avg/min/max-style
        aggregates. Defaults to all numeric/boolean/date/string fields
        on the model.
    group_by_fields : fields eligible for ``group_by``. Defaults to all
        plain (non-many) fields plus FK references.
    operators : per-field operator overrides. Keys are field names;
        values are tuples of permitted :class:`AggregateOp`.
    name_prefix : optional prefix for emitted type names (defaults to
        ``model.__name__``).
    filter_type : optional strawberry-django filter type to accept on
        the emitted resolver fields. When ``None``, no filter argument
        is added.
    """

    model:            type[Model]
    aggregate_fields: list[str] | None = None
    group_by_fields:  list[str] | None = None
    operators:        dict[str, tuple[AggregateOp, ...]] = dc_field(
        default_factory=dict,
    )
    name_prefix:      str | None = None
    filter_type:      type | None = None

    def build(self) -> "BuiltAggregates":
        """Generate all types and return them along with attached fields.

        Returns
        -------
        BuiltAggregates
            Bundle with the emitted types and two ready-to-use fields:
            ``aggregate_field`` and ``group_by_field``.

        Raises
        ------
        ValueError
            On invalid configuration (unknown fields, conflicting
            allowlists, etc.).
        """
        raise NotImplementedError(
            "Implementation pending — see docs/SPEC.md"
        )


@dataclass
class BuiltAggregates:
    """Output of :meth:`AggregateBuilder.build`.

    Attributes
    ----------
    aggregate_type : the ``<Model>Aggregate`` strawberry type
    grouped_type : the ``<Model>Grouped`` strawberry type
    grouped_result_type : the ``<Model>GroupedResult`` strawberry type
    group_key_type : the ``<Model>GroupKey`` strawberry type
    having_input : the ``<Model>Having`` strawberry input
    group_by_spec : the ``<Model>GroupBySpec`` strawberry input
    groupable_field_enum : the ``<Model>GroupableField`` enum
    aggregate_field : strawberry field that returns ``aggregate_type``
    group_by_field : strawberry field that returns ``grouped_result_type``
    """

    aggregate_type:       type
    grouped_type:         type
    grouped_result_type:  type
    group_key_type:       type
    having_input:         type
    group_by_spec:        type
    groupable_field_enum: type
    aggregate_field:      Any
    group_by_field:       Any
