# Changelog

All notable changes to `strawberry-django-aggregates` are documented here.
The project follows [Semantic Versioning](https://semver.org/). During the
`0.x` line, minor releases may include controlled breaking changes; see
`docs/SPEC.md` § 16 for the eventual 1.0 SemVer surface.

## [0.4.0] — 2026-06-02

### Added

- **Grouped filter echo (opt-in).** A new `enable_filter_echo=True` flag on
  `AggregateBuilder` adds a `filter: JSON!` field to each grouped bucket — a
  value shaped exactly like the list query's `filter:` argument that
  re-selects that bucket's rows, so a client can drill from a bucket into the
  underlying list. Requires `filter_type`; applies to both the offset and
  cursor grouped fields; computed lazily (only when `filter` is selected).
  Default `False` keeps SDL byte-identical to a non-echo build (Critical
  Rule 2). SPEC § 4.4.
  - The bucket→filter translation reuses the live `filter_type` rather than
    hardcoding names: filter field and lookup names (`exact`, `pk`, `gte`,
    `lt`, `is_null`) are resolved against the type and **fail loud** when
    absent, so a strawberry-django filter-shape change is a testable error,
    not corrupt output. Wire casing is delegated to `to_camel_case`; the
    half-open `{gte, lt}` interval is read from the already-computed
    `BucketRange` (never strawberry-django's inclusive `range`).
  - Value form mirrors what strawberry-django's default lookups expect: a
    `choices` column echoes its **stored** value (`paid`), not the enum
    member name the group key serializes to (`PAID`); a foreign-key axis
    echoes `{ <fk>: { pk: <id> } }` (the relation-filter shape); dates use
    ISO-8601; `Decimal` falls back to its string form. NULL keys echo
    `{ isNull: true }`. Repeated axes on one field fold into a nested `AND`
    so no clause is dropped.
- **`FilterEchoError`** — new fail-loud error in the `AggregateError`
  hierarchy, re-exported from the package root. Raised when a bucket cannot
  be faithfully expressed as a list filter: NUMBER-granularity buckets
  (disjoint ranges, no single interval), JSON-path group axes (no matching
  GraphQL input field), or a `filter_type` missing the required field /
  lookup.

## [0.3.0] — 2026-05-31

### Added

- **Choices-backed group-by enums.** A `group_by` field declared with
  Django `choices` now surfaces on `<Model>GroupKey` as a typed GraphQL
  enum (`OrderStatus`) instead of its base `String` / `Int` scalar, and the
  grouped resolver coerces each row's raw stored value to the matching enum
  member (the wire serializes the member name, e.g. `"PAID"`). Member names
  derive from a django-choices-field `choices_enum` verbatim when present
  (read via `getattr` — no dependency on the package), otherwise from the
  stored value, falling back to the label for empty / digit-leading values
  (integer choices `1 / "Low"` → `LOW`). The enum is built deterministically
  and cached per `(prefix, field.name)`. Date / datetime / time, FK, and
  JSON-path columns are unaffected. SPEC § 4.3.
- **`ChoicesEnumCollisionError`, `ChoicesEnumNameError`,
  `ChoicesValueNotInEnumError`** — new fail-loud errors in the
  `AggregateError` hierarchy, re-exported from the package root. Raised,
  respectively, when two choices collapse to the same member name or stored
  value, when no legal member name can be derived, and when a stored row
  value is outside the field's declared choices.

### Changed

- **(Controlled breaking, SDL.)** `<Model>GroupKey` columns for `choices`
  fields change type from `String` / `Int` to the per-field enum. Consumers
  that grouped by a choices column now receive the enum member name on the
  wire rather than the raw stored value. Cursor-pagination keysets still
  operate on the raw stored values (§ 4.1), so decoded cursors map back
  through the emitted enum to recover the wire name.

## [0.2.2] — 2026-05-09

### Fixed

- Aligned package metadata and the exported `__version__` with the 0.2.2
  release tag.
- Added tag-driven PyPI publishing through the repository `PYPI_TOKEN` secret.
- Updated PyPI project links to the `fyltr/strawberry-django-aggregates`
  repository.

## [0.2.1] — 2026-05-01

The beta line closing the gap analysis vs Odoo 18 / Hasura / PostGraphile,
bringing every former non-goal into scope, and stabilising the operator
vocabulary, granularity track, and SDL emission contract for early adopters.

### Added

- **`BigInt` scalar** — string-encoded 64-bit integer; `SUM` over
  `IntegerField` / `SmallIntegerField` / `PositiveIntegerField` /
  `PositiveSmallIntegerField` now emits `BigInt` so JS clients past
  `Number.MAX_SAFE_INTEGER` (2⁵³) survive end-to-end. SPEC § 5.
- **`stddev_pop` / `var_pop`** population-variance operators alongside the
  existing sample variants. Postgres-only.
- **`percentile_cont(field, fraction)`**, **`percentile_disc(field, fraction)`**,
  and **`mode`** (PG ordered-set aggregates). Method-style wire fields for
  the percentile pair; `mode` follows the regular `<Model>ModeFields`
  nested-type pattern.
- **`count_distinct(fields: [Enum!]!)`** Hasura-style multi-column distinct
  emitting `COUNT(DISTINCT (a, b, c))` on PG and a NULL-coalesced
  concatenation emulation on SQLite. New `AggregateOp.COUNT_DISTINCT_TUPLE`
  enum member.
- **`every` / `some`** SQL-standard wire aliases for `bool_and` / `bool_or`.
- **`BucketRange { from, to }`** half-open interval siblings on
  `<Model>GroupKey` for every `TimeGranularity` bucket. New `bucket_range`
  primitive callable from non-GraphQL contexts.
- **Locale-aware `week_start`** — `weekStart: Int = 1` arg on the grouped
  field shifts the first day of the week (1 = Monday … 7 = Sunday) for
  `WEEK` and `DAY_OF_WEEK`. Mirrors Odoo `models.py:2142–2168`.
- **`fill_temporal`** empty-bucket filling. `fill: Boolean = false`,
  `fillMin: DateTime`, `fillMax: DateTime` on the grouped resolver. Pure-
  Python merge for portability across PG / SQLite.
- **Cursor pagination on grouped results** — additive Relay-style
  `<Model>GroupedConnection` alongside the existing offset-based
  `<Model>GroupedResult`. Builder kwarg `pagination_style` (`"offset"`
  default, `"cursor"`, or `"both"`). New `encode_group_cursor` /
  `decode_group_cursor` primitives.
- **Apollo Federation v2 directives** — opt-in `enable_federation: bool` on
  `AggregateBuilder` switches emitted types to `strawberry.federation.type`
  and decorates FK group-key fields with `@external`. `@key` and
  `@requires` / `@provides` deferred to v1.x.
- **Streaming chunked group-by** — `chunk_size: int | None = None` kwarg on
  `compute_aggregation` returns an iterator of result batches paginated via
  keyset on the canonical group-by tuple. Backend-only; not exposed on the
  GraphQL surface.
- **Cross-relation aggregate field** —
  `register_relation_aggregate(parent_type, "children", child_built)`
  attaches `<children>Aggregate(filter: ...)` to existing strawberry-django
  parent types. Per-row resolver in v1.0; dataloader batching in v1.x.
- **`allow_relation_traversal: bool = False`** opt-in on
  `compute_aggregation` accepts `__`-traversing field paths and emits
  `Subquery`-wrapped per-row aggregates that do not row-multiply.
  Restricted to `SUM/AVG/MIN/MAX/COUNT/COUNT_DISTINCT` in v1.0; default
  refusal preserved per Critical Rule 4.
- **`respect_comodel_ordering: bool = False`** opt-in on
  `compute_aggregation` and `AggregateBuilder` traverses the comodel's
  `Meta.ordering` when ordering by an FK group-by alias. Mirrors Odoo
  `_order_field_to_sql:2253`. New `comodel_ordering_terms` helper.
- **JSONB property groupby and aggregation** — `json_paths={"metadata.amount":
  "Decimal", ...}` on `AggregateBuilder` and `compute_aggregation` accepts
  typed dotted-path access on `JSONField` columns. Group_by, aggregation,
  HAVING, and ordering all route through the JSON path. New
  `JSONPathNotAllowed` error and `default_operators_for_json_type` helper.
- **NULL semantics documented** in SPEC § 5 — every operator's behaviour on
  NULL inputs and empty groups now explicit.
- **`HavingFieldNotAllowed` / `GroupByFieldNotAllowed` /
  `GranularityNotApplicable`** error classes now exported from the package
  root for consumers writing typed-error GraphQL extensions.

### Changed (breaking)

- **`SUM(IntegerField)` SDL output type** changed from `Int` to `BigInt`.
  Clients re-typegen. See `docs/MIGRATING.md`.
- **`AggregateOp` enum gained 5 new members** (`STDDEV_POP`, `VAR_POP`,
  `PERCENTILE_CONT`, `PERCENTILE_DISC`, `MODE`, `COUNT_DISTINCT_TUPLE`).
  Canonical-emission order is now part of the SemVer surface (SPEC § 12);
  reordering existing members in a future release would be a major bump.
- **`compute_aggregation` return type** widens to `list[dict] |
  Iterator[list[dict]]` — only callers using `chunk_size` see the iterator
  variant; the default `None` keeps `list[dict]` semantics.
- **CLAUDE.md Critical Rule 4** amended to reference the new
  `allow_relation_traversal` opt-in.

### Out of scope for 0.2.x (deferred to 1.x)

- **Window functions** (`ROW_NUMBER`, `RANK`, `LAG`, `LEAD`, running
  aggregates) — v1.1.
- **Federation `@key` / `@requires` / `@provides` directives on aggregate
  result containers** — v1.1.
- **Dataloader-based batching for cross-relation aggregate field** — v1.x.
- **Multi-valued JSONB arrays via `jsonb_array_elements`** (Odoo properties
  tags / m2m equivalent) — v1.x.

### Internal

- 11 source modules, 16 test files, 245 tests passing on SQLite (7 PG-only
  tests skipped), `ruff` and `mypy` clean.
- Full `compiler.py` zero-GraphQL-coupling property preserved (Critical
  Rule 9). Permission-naive design preserved (Critical Rule 1).

## [0.1.0] — 2026-04-XX

Initial draft release; consumed internally by `django-angee`. See git
history for the v0.1 surface.
