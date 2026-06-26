"""Empty-bucket filling for date-bucketed aggregations — SPEC § 7.2.

Given a list of aggregation result rows whose ``group_by`` spec contains
exactly ONE ``TimeGranularity`` entry, generate the dense bucket spine
between the data's min and max (or explicit ``fill_min`` / ``fill_max``
overrides), and emit one row per missing bucket with ``count: 0`` and all
non-key measures left as ``None``.

Implementation strategy: post-process Python merge rather than SQL
``generate_series`` + ``LATERAL JOIN``. Reasons:

- Works uniformly on PostgreSQL and SQLite (no vendor-specific SQL).
- Easier to test and audit.
- Keeps the SQL plan simple — ``generate_series`` + LEFT JOIN would
  interact poorly with HAVING (HAVING would be applied to the join
  output and exclude any zero-count bucket, defeating the filling).
- Cardinality is bounded for analytics-shaped queries; bucket counts
  rarely exceed 10000 even on multi-year ``DAY``-grain queries.

CLAUDE.md Critical Rule 9 — this module is framework-agnostic. Pure
stdlib + the pure ``aliasing`` / ``granularity`` modules. No Django,
no Strawberry.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from typing import Any

from strawberry_django_aggregates.aliasing import group_by_alias
from strawberry_django_aggregates.granularity import (
    Granularity,
    TimeGranularity,
    validate_week_start,
)

# ---------------------------------------------------------------------------
# Spine generation
# ---------------------------------------------------------------------------


def _add_months(value: datetime.datetime, months: int) -> datetime.datetime:
    """Add ``months`` to ``value`` preserving day=1 / time-of-day.

    ``value`` is always a bucket start (``date_trunc``'d already), so the
    Odoo-style "clamp day to month length" logic is unnecessary. Mirrors
    the helper of the same name in ``compiler.py``; kept private here so
    this module has no dependency back on the compiler.
    """
    total = value.month - 1 + months
    new_year = value.year + total // 12
    new_month = total % 12 + 1
    return value.replace(year=new_year, month=new_month)


def _advance(
    value: datetime.datetime, granularity: TimeGranularity,
) -> datetime.datetime:
    """Return the bucket-start immediately following ``value``.

    Mirrors :func:`compiler.bucket_range`'s right edge, but as a single
    value rather than a tuple. Kept local so this module stays
    framework-agnostic and importable in isolation.
    """
    if granularity is TimeGranularity.YEAR:
        return _add_months(value, 12)
    if granularity is TimeGranularity.QUARTER:
        return _add_months(value, 3)
    if granularity is TimeGranularity.MONTH:
        return _add_months(value, 1)
    if granularity is TimeGranularity.WEEK:
        return value + datetime.timedelta(days=7)
    if granularity is TimeGranularity.DAY:
        return value + datetime.timedelta(days=1)
    if granularity is TimeGranularity.HOUR:
        return value + datetime.timedelta(hours=1)
    if granularity is TimeGranularity.MINUTE:
        return value + datetime.timedelta(minutes=1)
    if granularity is TimeGranularity.SECOND:
        return value + datetime.timedelta(seconds=1)
    raise ValueError(  # defensive — exhaustive over TimeGranularity
        f"Unknown TimeGranularity {granularity!r}.",
    )


def _floor(
    value: datetime.datetime,
    granularity: TimeGranularity,
    week_start: int = 1,
) -> datetime.datetime:
    """Truncate ``value`` to the start of its bucket at ``granularity``.

    Mirrors PostgreSQL's ``date_trunc`` for the supported granularities.
    Used to align the spine endpoints when the caller passes a non-aligned
    ``fill_min`` / ``fill_max`` (e.g. an arbitrary "now" datetime). Result
    preserves ``tzinfo``.
    """
    if granularity is TimeGranularity.YEAR:
        return value.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0,
        )
    if granularity is TimeGranularity.QUARTER:
        # Quarter boundary: month rounded down to {1, 4, 7, 10}.
        q_month = ((value.month - 1) // 3) * 3 + 1
        return value.replace(
            month=q_month, day=1, hour=0, minute=0, second=0, microsecond=0,
        )
    if granularity is TimeGranularity.MONTH:
        return value.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0,
        )
    if granularity is TimeGranularity.WEEK:
        validate_week_start(week_start)
        # Python ``isoweekday()`` is 1=Mon..7=Sun. Shift so the user's
        # ``week_start`` lands at offset 0.
        iso_dow = value.isoweekday()
        offset = (iso_dow - week_start) % 7
        midnight = value.replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        return midnight - datetime.timedelta(days=offset)
    if granularity is TimeGranularity.DAY:
        return value.replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
    if granularity is TimeGranularity.HOUR:
        return value.replace(minute=0, second=0, microsecond=0)
    if granularity is TimeGranularity.MINUTE:
        return value.replace(second=0, microsecond=0)
    if granularity is TimeGranularity.SECOND:
        return value.replace(microsecond=0)
    raise ValueError(  # defensive
        f"Unknown TimeGranularity {granularity!r}.",
    )


def generate_bucket_spine(
    min_dt: datetime.datetime,
    max_dt: datetime.datetime,
    granularity: TimeGranularity,
    week_start: int = 1,
) -> Iterator[datetime.datetime]:
    """Yield each bucket-start datetime from ``min_dt`` (inclusive) up to
    and including the bucket containing ``max_dt``.

    Both endpoints are floored to the granularity bucket they belong to,
    so callers can pass arbitrary "current time"-style datetimes for
    ``fill_min`` / ``fill_max`` without needing to ``date_trunc`` them
    first. ``tzinfo`` is preserved — both endpoints must share the same
    ``tzinfo`` (or both be naive); a mismatch raises ``ValueError``.

    The output cardinality is bounded by ``ceil((max - min) / step)``
    where ``step`` is one bucket. For analytics-shaped queries the count
    is rarely > 10000 even on multi-year ``DAY``-grain queries, so this
    iterator is materialized eagerly by callers.
    """
    if (min_dt.tzinfo is None) != (max_dt.tzinfo is None):
        raise ValueError(
            "fill_min and fill_max must agree on tzinfo "
            "(both naive or both aware).",
        )
    validate_week_start(week_start)

    start = _floor(min_dt, granularity, week_start)
    stop = _floor(max_dt, granularity, week_start)
    if start > stop:
        return  # empty spine — caller can short-circuit
    cursor = start
    while cursor <= stop:
        yield cursor
        cursor = _advance(cursor, granularity)


# ---------------------------------------------------------------------------
# Result merging
# ---------------------------------------------------------------------------


def fill_bucket_results(
    rows: list[dict[str, Any]],
    group_by_spec: list[tuple[str, Granularity | None]],
    aggregate_aliases: list[str],
    fill_min: datetime.datetime | None,
    fill_max: datetime.datetime | None,
    week_start: int = 1,
) -> list[dict[str, Any]]:
    """Merge ``rows`` with a dense bucket spine, returning a sorted list.

    Pre-conditions (caller-enforced; we re-validate defensively):

    - ``group_by_spec`` contains exactly ONE entry whose granularity is a
      :class:`TimeGranularity` member. Multi-level group_by + fill is a
      v1.x feature; v1.0 raises before reaching this function.
    - ``rows`` already had HAVING applied (the caller filtered before
      filling — see SPEC § 7.2 for the ordering decision).

    Behaviour:

    - The spine spans ``[min(rows), max(rows)]`` unless ``fill_min`` /
      ``fill_max`` override either endpoint.
    - Filled rows have ``"count": 0`` and every other aggregate alias
      set to ``None``. Group-by aliases for non-bucket entries are
      copied from the bucket key… wait — there are no non-bucket
      entries in v1.0 by precondition. The single bucket alias is set
      to the spine value.
    - Output is sorted ascending by the bucket alias.

    This function is pure (no side effects; stdlib + pure helpers) — it
    can be called from any Python context. CLAUDE.md Critical Rule 9.
    """
    # Locate the single TIME-granularity bucket entry. Caller has already
    # validated this; the second pass here is defensive. ``group_by_alias``
    # is the single owner of the bucket-alias rule (SPEC § 16) — deriving
    # the key here rather than recomputing ``f"{field_path}_{grain}"``
    # keeps this lookup in lockstep with the alias the compiler annotated
    # (notably the JSON ``.`` → ``__`` rewrite).
    bucket_alias: str | None = None
    bucket_grain: TimeGranularity | None = None
    for field_path, granularity in group_by_spec:
        if isinstance(granularity, TimeGranularity):
            if bucket_alias is not None:
                raise ValueError(
                    "fill=True requires exactly one TIME-granularity "
                    "bucket in group_by; found multiple.",
                )
            bucket_alias = group_by_alias(field_path, granularity)
            bucket_grain = granularity
    if bucket_alias is None or bucket_grain is None:
        raise ValueError(
            "fill=True requires exactly one TIME-granularity bucket "
            "in group_by; found none.",
        )

    # Build lookup: bucket value → row dict (already keyed by alias).
    by_bucket: dict[datetime.datetime, dict[str, Any]] = {}
    for row in rows:
        value = row.get(bucket_alias)
        if isinstance(value, datetime.datetime):
            by_bucket[value] = row
        # Rows with NULL bucket value (rare — only possible if the
        # underlying date column has NULLs and the user didn't filter
        # them out) are passed through unchanged at the end.

    # Determine spine endpoints. Use explicit overrides if given;
    # otherwise derive from the data. If neither the data nor the
    # overrides supply both endpoints, we have nothing to fill — return
    # the rows as-is (sorted).
    data_min = min(by_bucket.keys()) if by_bucket else None
    data_max = max(by_bucket.keys()) if by_bucket else None
    spine_min = fill_min if fill_min is not None else data_min
    spine_max = fill_max if fill_max is not None else data_max
    if spine_min is None or spine_max is None:
        return _sorted_with_nulls_last(rows, bucket_alias)

    # Align overrides to the granularity floor in the SAME tzinfo as the
    # data. When the caller passed an explicit endpoint with a different
    # tzinfo than the data's bucket values, normalize to the data's
    # tzinfo so the spine and the actual rows align byte-identical.
    sample_tz = data_min.tzinfo if data_min is not None else spine_min.tzinfo
    spine_min = _coerce_tz(spine_min, sample_tz)
    spine_max = _coerce_tz(spine_max, sample_tz)

    spine_min = _floor(spine_min, bucket_grain, week_start)
    spine_max = _floor(spine_max, bucket_grain, week_start)

    # Merge: walk the spine, emit either the actual row or a zero-count
    # filler. Preserves all existing keys from each actual row; the
    # filler row's keys are exactly the bucket alias and the aggregate
    # aliases (other group-by aliases don't appear in the v1.0 single-
    # TIME-granularity contract).
    out: list[dict[str, Any]] = []
    for spine_value in generate_bucket_spine(
        spine_min, spine_max, bucket_grain, week_start,
    ):
        existing = by_bucket.pop(spine_value, None)
        if existing is not None:
            out.append(existing)
        else:
            filler: dict[str, Any] = {bucket_alias: spine_value}
            for alias in aggregate_aliases:
                filler[alias] = 0 if alias == "count" else None
            out.append(filler)

    # Any row whose bucket value sat OUTSIDE the spine (e.g. fill_min /
    # fill_max narrowed the window) is preserved at the end — dropping
    # it would be silent data loss. Sort the leftovers by bucket value
    # so the output stays monotonic; ``None`` bucket values fall last.
    if by_bucket:
        leftovers = list(by_bucket.values())
        out.extend(_sorted_with_nulls_last(leftovers, bucket_alias))

    return out


def _coerce_tz(
    value: datetime.datetime, tz: datetime.tzinfo | None,
) -> datetime.datetime:
    """Normalize ``value``'s ``tzinfo`` to ``tz`` for comparison.

    If ``tz`` is None and ``value`` is aware, drop the tz; if ``tz`` is
    set and ``value`` is naive, attach ``tz`` (without converting the
    wall-clock instant). If both are aware, convert ``value`` to ``tz``
    so the bucket arithmetic operates in a single frame of reference.
    """
    if tz is None and value.tzinfo is None:
        return value
    if tz is None:  # aware → naive
        return value.replace(tzinfo=None)
    if value.tzinfo is None:  # naive → aware (interpret as ``tz``)
        return value.replace(tzinfo=tz)
    if value.tzinfo is tz:
        return value
    return value.astimezone(tz)


def _sorted_with_nulls_last(
    rows: list[dict[str, Any]], bucket_alias: str,
) -> list[dict[str, Any]]:
    """Sort rows by ``bucket_alias`` ascending; ``None`` values go last.

    Mirrors the SQL ``ORDER BY <alias> ASC NULLS LAST`` semantic the
    GraphQL resolver applies by default. ``None`` is sorted via a
    sentinel tuple so Python doesn't ``TypeError`` comparing ``None``
    to ``datetime``.
    """
    def keyfn(row: dict[str, Any]) -> tuple[int, datetime.datetime | None]:
        v = row.get(bucket_alias)
        return (1, None) if v is None else (0, v)

    return sorted(rows, key=keyfn)
