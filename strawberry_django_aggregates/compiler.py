"""Backend primitive — :func:`compute_aggregation`.

Mirrors Odoo's ``BaseModel._read_group`` (``odoo/models.py:~3923``):
backend-first API returning flat composite-key result rows. Callable
from any Python context — DRF view, Celery task, admin script, MCP tool
— not just GraphQL resolvers.

Separation of concerns: this primitive returns rows; the GraphQL
resolver (in :mod:`strawberry_django_aggregates.builder`) is the
presentation wrapper that shapes those rows into the Strawberry types.
"""

from __future__ import annotations

from typing import Any

from django.db.models import QuerySet

from strawberry_django_aggregates.granularity import Granularity
from strawberry_django_aggregates.operators import AggregateOp


def compute_aggregation(
    queryset: QuerySet,
    *,
    group_by:   list[tuple[str, Granularity | None]] | None = None,
    aggregates: list[tuple[AggregateOp, str | None]] | None = None,
    having:     dict[str, Any] | None = None,
    order_by:   list[tuple[str, str, str | None]] | None = None,
    offset:     int = 0,
    limit:      int | None = None,
    tz:         str | None = None,
) -> list[dict[str, Any]]:
    """Compile a queryset into an aggregation query.

    Parameters
    ----------
    queryset : Django QuerySet
        Must already be permission-scoped by the caller (e.g. via
        ``accessible_by(user)`` or ``filter(owner=request.user)``).
        This library does not enforce row-level access.
    group_by : list of ``(field_path, granularity)`` tuples
        ``granularity`` is ``None`` for non-date fields and either a
        :class:`TimeGranularity` or :class:`NumberGranularity` member
        for date/datetime fields.
    aggregates : list of ``(op, field_path)`` tuples
        ``field_path`` is ``None`` for ``COUNT``. Postgres-only ops
        raise :class:`OperatorNotSupportedError` on SQLite connections.
    having : dict of ``"<measure>__<lookup>": value`` entries
        ``measure`` is ``count``, ``count_distinct``, or
        ``"<op>_<field>"``. ``lookup`` is one of ``gt``, ``lt``,
        ``gte``, ``lte``, ``eq``, ``neq``, ``in``, ``not_in``.
    order_by : list of ``(field_or_alias, direction, nulls)`` tuples
        ``field_or_alias`` resolves against group-by fields and
        aggregate aliases. Unknown values raise
        :class:`OrderFieldNotAllowed`.
    offset, limit : integer pagination
    tz : IANA timezone name (e.g. ``"Asia/Tokyo"``)
        Applied as ``timezone(tz, timezone('UTC', col))`` *before*
        ``date_trunc`` for any date-bucketed group_by. When ``None``,
        ``settings.TIME_ZONE`` is used.

    Returns
    -------
    list of dicts
        One row per group bucket. Keys = group-by field paths
        (truncated/extracted as requested) plus aggregate aliases
        (``count``, ``count_distinct``, ``sum_<field>``, etc.).
        Multi-level group-by produces multiple rows with composite
        keys; client-side folding for tree UIs is the caller's job.

    Raises
    ------
    OperatorNotSupportedError
        Postgres-only operator on a non-Postgres connection.
    AggregationAcrossRelationError
        ``field_path`` traverses a one-to-many or m2m relation.
    OrderFieldNotAllowed
        ``order_by`` references an unknown alias.
    GroupByFieldNotAllowed
        ``group_by`` references an unknown field.
    HavingFieldNotAllowed
        ``having`` references an unknown alias.
    GranularityNotApplicable
        ``granularity`` is set on a non-date/non-datetime field.
    """
    raise NotImplementedError("Implementation pending — see docs/SPEC.md")
