# Contributing

Thank you for contributing to `strawberry-django-aggregates`.

## Quality gate (run before opening a PR)

Use the same command chain locally that CI enforces:

```bash
uv run ruff check .
uv run mypy strawberry_django_aggregates/
uv run pytest
```

## Recommended failure triage order

1. `ruff` (fastest feedback; formatting/lint correctness)
2. `mypy` (type-safety regressions)
3. `pytest` (behavior/cross-module integration)

## Version/runtime expectations

- Python: 3.13
- Django: 6.0
- Backends: SQLite (always) and PostgreSQL (Postgres-only operators
  raise `OperatorNotSupportedError` at resolver entry on SQLite — see
  `docs/SPEC.md` § 5).

## Determinism expectations

Any change that affects type generation must preserve deterministic SDL:

- same inputs must emit byte-identical schema output,
- avoid non-deterministic ordering in iteration (sort dict/set traversal),
- avoid time/PRNG-dependent values in emitted type names or defaults.

See `CLAUDE.md` Critical Rule 2 and `docs/SPEC.md` § 12 for the full
determinism contract.

## Golden SDL

`tests/test_builtin_sdl.py` pins the group key and measure types of the test
models whose fields are all Django built-ins against
`tests/golden/builtin_fields.graphql`. Regenerate it with
`uv run python tests/golden_sdl.py`, from a checkout of `v0.14.1` with the
current `tests/` copied in, unless this library means to change those fields.
A diff after a dependency bump is the SDL printer's formatting: check it against
`v0.14.1`'s semantics before accepting it.
