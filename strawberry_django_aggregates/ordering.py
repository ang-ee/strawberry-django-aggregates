"""Aggregate-aware order parser.

Resolves order terms against three namespaces in this priority:

1. Aggregate aliases — ``count``, ``count_distinct``, ``<op>_<field>``.
2. Group-by field paths — including bucketed forms like
   ``created_at_month``.
3. Plain model field paths declared in the order-by allowlist.

Unknown terms raise :class:`OrderFieldNotAllowed`. Mirrors Odoo's
post-17 fail-loud behaviour — the pre-17 ``read_group`` silently dropped
unknown terms and was a recurring source of "why isn't this query
ordered?" bug reports.
"""

from __future__ import annotations

from typing import Literal


def parse_aggregate_order(
    term: str,
    *,
    group_by_fields: list[str],
    aggregate_aliases: list[str],
    field_allowlist: list[str] | None = None,
) -> tuple[str, Literal["asc", "desc"]]:
    """Parse a single order term into ``(canonical_alias, direction)``.

    Accepted forms:

    - ``"field"``                  — ascending plain field
    - ``"-field"``                 — descending plain field
    - ``"field desc"``             — explicit direction
    - ``"<op>_<field>"``           — aggregate alias (e.g. ``sum_total``)
    - ``"field:<op>"``             — Odoo-flavored aggregate alias
    - ``"field:granularity"``      — bucketed groupBy reference

    Parameters
    ----------
    term : the order string
    group_by_fields : aliases produced by the active ``group_by``
        clause (including bucket suffixes like ``created_at_month``)
    aggregate_aliases : the set of valid aggregate aliases for the
        active query (e.g. ``["count", "sum_total", "avg_total"]``)
    field_allowlist : optional plain-field names allowed when no
        aggregation is active

    Returns
    -------
    ``(canonical, direction)`` where ``canonical`` is the queryset
    annotation name and ``direction`` is ``"asc"`` or ``"desc"``.

    Raises
    ------
    OrderFieldNotAllowed
        ``term`` does not match any allowlist.
    """
    raise NotImplementedError("Implementation pending — see docs/SPEC.md")


def aggregate_aliases_from_spec(
    aggregates: list[tuple[str, str | None]],
) -> list[str]:
    """Compute the alias names that :func:`compute_aggregation` will
    emit for a given aggregate spec.

    Used by the ordering parser and by the HAVING input validator to
    resolve which aliases are addressable.
    """
    raise NotImplementedError("Implementation pending — see docs/SPEC.md")
