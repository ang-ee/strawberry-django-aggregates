"""Exception hierarchy for strawberry-django-aggregates.

All errors descend from :class:`AggregateError` so consumers can catch
the family without depending on individual subclasses.
"""

from __future__ import annotations


class AggregateError(Exception):
    """Base class for all errors raised by strawberry-django-aggregates."""


class OperatorNotSupportedError(AggregateError):
    """Raised when an operator is unsupported on the current database vendor.

    Postgres-only operators (``array_agg``, ``string_agg``, ``stddev``,
    ``variance``) raise this at resolver entry on SQLite connections —
    not at SQL execution time. The message names the operator and the
    detected connection vendor.
    """


class OrderFieldNotAllowed(AggregateError):
    """Raised when an order term does not resolve to a known field or alias.

    Mirrors Odoo's post-17 fail-loud behaviour. The pre-17 implementation
    silently dropped unknown terms; we refuse the request instead.
    """


class AggregationAcrossRelationError(AggregateError):
    """Raised when an aggregate measure references a one-to-many / m2m path.

    By default, auto-traversal is refused: it would cause silent row
    multiplication corrupting every measure in the same query. The
    canonical alternative is to query the child model with the parent FK
    in ``group_by``. ``array_agg`` is the explicit escape hatch for
    "give me child IDs per parent group."

    For callers that genuinely need a measure across a one-to-many or
    many-to-many relation, the backend primitive
    :func:`strawberry_django_aggregates.compute_aggregation` accepts
    ``allow_relation_traversal=True``. When set, the compiler emits a
    correlated ``Subquery`` per measure (one ``Subquery`` per measure,
    not a JOIN), which avoids row-multiplication. This flag lives only
    on the primitive — it is intentionally not surfaced through
    ``AggregateBuilder`` / GraphQL (Critical Rule 9 separation).
    """


class HavingFieldNotAllowed(AggregateError):
    """Raised when a HAVING input references an unknown aggregate alias."""


class GroupByFieldNotAllowed(AggregateError):
    """Raised when a group-by spec references a field not in the allowlist."""


class GranularityNotApplicable(AggregateError):
    """Raised when granularity is set on a non-date / non-datetime field."""


class JSONPathNotAllowed(AggregateError):
    """Raised when a dotted JSON path is not in the caller's allowlist.

    The first segment of a dotted ``metadata.region`` path resolves to a
    Django ``JSONField`` on the model, but the full path was not declared
    in the ``json_paths`` allowlist passed to
    :func:`compute_aggregation` / :class:`AggregateBuilder`. Mirrors the
    fail-loud semantics of :class:`GroupByFieldNotAllowed` — opting in to
    a JSON path is explicit, never auto-discovered. See SPEC § 6.1.
    """


class ChoicesEnumCollisionError(AggregateError):
    """Raised when two choices collapse into the same enum member.

    A ``choices=[(value, label), ...]`` group-by column is emitted as a
    GraphQL enum whose member names are derived from the stored values
    (or labels, for empty / digit-leading values). Two choices collide
    when they derive the same member NAME, or when they share the same
    stored VALUE — which Python's ``enum`` would silently alias, dropping
    one choice from the schema. Either way we refuse to silently
    deduplicate: that would drop a choice and quietly mis-coerce rows.
    Fail loud instead (CLAUDE.md fail-loud / strict-whitelist stance):
    rename the colliding choice or supply a django-choices-field
    ``choices_enum`` with explicit member names. See SPEC § 4.3.
    """


class ChoicesEnumNameError(AggregateError):
    """Raised when a choice yields no valid GraphQL enum member name.

    Each plain-``choices`` member name is derived from the stored value
    (uppercased, non-identifier chars → ``_``), falling back to the label
    when the value sanitizes to an empty or digit-leading identifier
    (integer choices ``1 / "Low"`` → ``LOW``). When BOTH the value and the
    label sanitize to an empty or digit-leading name (e.g. ``1 / "1st"`` or
    ``2 / ""``), no legal member name can be derived. ``enum.Enum`` would
    raise a bare ``ValueError``; we fail loud with a typed, actionable
    error instead: rename the label or supply a django-choices-field
    ``choices_enum`` with explicit member names. See SPEC § 4.3.
    """


class ChoicesValueNotInEnumError(AggregateError):
    """Raised when a grouped row's stored value is not among the choices.

    A ``choices``-backed group-by column is emitted as a GraphQL enum, and
    the resolver coerces each row's RAW stored value to the matching enum
    member. Django's ``choices`` is a form/validation concern, not a
    database constraint, so a column may legally hold a value no longer in
    the declared choices (e.g. a retired status on historical rows). Such
    a value cannot be coerced; we fail loud with the field, the offending
    value, and the enum name rather than let a bare ``ValueError`` surface
    mid-serialization. Clean the data, re-add the value to ``choices``, or
    exclude the affected rows via the caller's queryset. See SPEC § 4.3.
    """


class FilterEchoError(AggregateError):
    """Raised when a grouped bucket cannot be echoed as a list filter.

    The opt-in ``enable_filter_echo`` flag adds a ``filter: JSON!`` field to
    each grouped bucket whose value re-selects that bucket's rows through
    the existing list query's ``filter_type`` (SPEC § 4.4). The bucket key
    cannot always be expressed as a faithful filter; we refuse loudly
    rather than emit a wrong-but-plausible filter that would silently
    select the wrong rows:

    - a NUMBER-granularity bucket (``month_of_year`` etc.) selects disjoint
      ranges across years and has no single-interval filter;
    - a JSON-path group axis (``metadata.region``) has no matching GraphQL
      input field on the list ``filter_type``;
    - the ``filter_type`` has no field for the group axis, or that field's
      lookup type does not expose the lookup the echo needs (``exact`` /
      ``gte`` / ``lt`` / ``is_null``).

    The last case is the load-bearing one: lookup names are resolved
    against the live ``filter_type`` rather than hardcoded, so a
    strawberry-django filter-shape change surfaces here as a testable error
    instead of corrupt JSON. The message names the field, and the missing
    lookup or the TIME-granularity alternative. See SPEC § 4.4.
    """
