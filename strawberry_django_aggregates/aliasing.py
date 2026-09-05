"""Canonical group-key output aliases — the single owner of the rule.

Pure string logic over the granularity enums: no Django or Strawberry
import at runtime (``Field`` is referenced only under ``TYPE_CHECKING``
and the FK branch duck-types it via ``getattr``). Keeping this in a leaf
module lets framework-agnostic consumers — notably :mod:`fill` — import
the rule at top level without pulling in the Django-heavy
:mod:`compiler`, and lets every alias-emitting site (the type emitter,
the resolver, cursor pagination, the having-echo, the dense-fill spine)
derive its wire keys from one place so they cannot drift (SPEC § 16).

``json_path_alias`` owns the JSON ``.`` → ``__`` rewrite;
``group_by_alias`` composes it with the FK ``_id`` / granularity suffixes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from strawberry_django_aggregates.granularity import (
    Granularity,
    TimeGranularity,
)

if TYPE_CHECKING:
    from django.db.models.fields import Field


def json_path_alias(field_path: str) -> str:
    """Convert a dotted JSON path to its Django-friendly alias.

    ``"metadata.region"`` → ``"metadata__region"``. Used as the kwarg
    name when annotating, the ``.values()`` argument when grouping, and
    the column name in the result row dict.

    The double-underscore separator matches Django's own convention for
    relation traversal aliases (``customer__name``); the alias never
    triggers Django's relation walker because it appears as an
    annotation kwarg, not as a path on a queryset's ``.filter`` /
    ``.values``.
    """
    return field_path.replace(".", "__")


def group_by_enum_member(field_path: str) -> str:
    """Canonical enum member for a declared group-by field path."""
    return json_path_alias(field_path).upper()


def group_by_alias(
    field_path: str,
    granularity: Granularity | None,
    field: Field | None = None,
) -> str:
    """Canonical output alias for a (field, granularity) pair.

    Public: the single owner of the group-key alias rule. The type
    emitter, the resolver, cursor pagination (§ 4.1), the having-echo
    (§ 4.3), and the dense-fill spine all derive their wire keys from
    this function so they cannot drift. Consumers building their own
    grouped envelope MUST call this rather than recompute the ``_id``
    suffix, the granularity suffix, or the JSON ``.`` → ``__`` rewrite.

    Dotted JSON paths are normalised to their column-alias form before
    any suffix is applied — delegating to :func:`json_path_alias`, the
    owner of the ``.`` → ``__`` rewrite (``metadata.region`` →
    ``metadata__region``). Django model-field paths never contain ``.``,
    so the rewrite is a no-op for them and existing model-field callers
    are unaffected.

    - ``("customer", None)`` with FK field → ``"customer_id"``
    - ``("status",   None)`` with plain field → ``"status"``
    - ``("created_at", TimeGranularity.MONTH)`` → ``"created_at_month"``
    - ``("created_at", NumberGranularity.DAY_OF_WEEK)`` →
      ``"created_at_day_of_week"``
    - ``("metadata.region", None)`` (JSON path) → ``"metadata__region"``
    - ``("metadata.created_at", TimeGranularity.MONTH)`` (JSON path) →
      ``"metadata__created_at_month"``
    """
    base = json_path_alias(field_path)
    if granularity is not None:
        return f"{base}_{granularity.value}"
    if field is not None and getattr(field, "is_relation", False) \
            and getattr(field, "many_to_one", False):
        return f"{base}_id"
    return base


def group_by_range_alias(
    field_path: str,
    granularity: TimeGranularity,
) -> str:
    """Canonical sibling alias for a temporal bucket's half-open range."""
    if not isinstance(granularity, TimeGranularity):
        raise ValueError(
            "group_by_range_alias requires a TimeGranularity value."
        )
    return f"{group_by_alias(field_path, granularity)}_range"
