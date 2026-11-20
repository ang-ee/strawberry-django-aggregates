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
`tests/golden/builtin_fields.graphql`. Unless this library means to change
those fields, regenerate it by running the `v0.14.1` package under the current
environment, with that package first on the path, from the current checkout:

```bash
mkdir -p /tmp/v0141
git archive v0.14.1 strawberry_django_aggregates | tar -x -C /tmp/v0141
.venv/bin/python -c "import sys; sys.path.insert(0, '/tmp/v0141'); \
from tests.conftest import pytest_configure; pytest_configure(); \
from tests.golden_sdl import GOLDEN, builtin_fields_sdl; \
GOLDEN.write_text(builtin_fields_sdl())"
```

Do not `uv run` inside an old checkout, whose `uv.lock` would resolve older
dependencies: the comparison must isolate the library, not the SDL printer. A
diff after a dependency bump is the printer's formatting: check it against
`v0.14.1`'s semantics before accepting it.
