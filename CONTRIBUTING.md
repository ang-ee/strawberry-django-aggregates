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

- Python: 3.13+
- Django: 5.0+
- Main backends: SQLite and PostgreSQL

## Determinism expectations

Any change that affects type generation must preserve deterministic SDL:

- same inputs should emit byte-identical schema output,
- avoid non-deterministic ordering in iteration,
- avoid time/PRNG-dependent values in emitted type names or defaults.
