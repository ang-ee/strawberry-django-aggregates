# ROADMAP

This roadmap captures **quality improvements and debt paydown only**.
It intentionally avoids adding new product features.

## 1) Align package maturity signals (Docs vs Metadata)

### Why
The public messaging is inconsistent:
- `README.md` says **"Alpha"** under Status.
- `pyproject.toml` declares `version = "1.0.0"` and classifier `Development Status :: 5 - Production/Stable`.

This can create support and adoption confusion (expectation mismatch about API stability and SemVer guarantees).

### Improvement
- Choose one maturity stance and apply it consistently across:
  - `README.md` status section.
  - `pyproject.toml` development status classifier.
  - `CHANGELOG.md` release notes language.

### Debt risk if deferred
- Consumer trust erosion and upgrade friction.

---

## 2) Reduce typing suppression footprint (`type: ignore`) with targeted wrappers

### Why
Core modules currently rely on many localized `type: ignore[...]` comments, especially in dynamic Strawberry type-construction paths.

This is expected in metaprogramming-heavy code, but the current footprint increases maintenance cost by hiding real regressions among necessary suppressions.

### Improvement
- Introduce small typed helper functions for repeated dynamic patterns (e.g., `list[dynamic_type]` and field assignment shims).
- Replace broad inline ignores with:
  - narrow helper APIs,
  - cast-based typing where semantically correct,
  - explicit protocol/type aliases for Strawberry runtime-decorated objects.
- Keep an allowlist of unavoidable ignore sites with rationale comments.

### Debt risk if deferred
- Mypy signal-to-noise degrades over time; true type regressions are harder to detect.

---

## 3) Split large modules to lower cognitive load and change risk

### Why
`builder.py` and `compiler.py` are large, multi-responsibility files (schema wiring, resolver policy, pagination logic, operator dispatch, SQL expression assembly).

Large files slow onboarding, increase merge conflicts, and make refactors riskier.

### Improvement
Refactor **without behavior changes** into focused modules, for example:
- `builder.py` → `builder/resolvers.py`, `builder/pagination.py`, `builder/introspection.py`.
- `compiler.py` → `compiler/grouping.py`, `compiler/ops.py`, `compiler/having.py`, `compiler/fill.py`.

Constraints:
- preserve public API surface and imports in `__init__.py`,
- keep deterministic SDL guarantees,
- keep `compiler` layer GraphQL-free per project rules.

### Debt risk if deferred
- Higher probability of accidental regressions during routine maintenance.

---

## 4) Codify backend-compatibility guarantees in test matrix

### Why
The codebase explicitly supports PostgreSQL + SQLite with deliberate degradation paths. That contract is strong in docs/spec, but CI drift can happen if backend coverage is not continuously enforced.

### Improvement
- Ensure CI has explicit lanes for:
  - SQLite full suite (already local default),
  - PostgreSQL suite including postgres-only operators,
  - deterministic schema emission checks.
- Add a "contract" test grouping/tag to make compatibility intent visible.

### Debt risk if deferred
- Silent backend-specific regressions and late discovery.

---

## 5) Normalize naming and wire-term vocabulary in docs

### Why
There are multiple closely related naming layers (snake_case ops, camelCase GraphQL aliases, SQL-standard synonyms `every`/`some`, and `count_distinct` variants).

Even when behavior is correct, scattered terminology increases misunderstanding for contributors.

### Improvement
- Add a single canonical terminology table in docs:
  - enum member,
  - Python API name,
  - GraphQL field name,
  - aliases/synonyms,
  - backend support notes.
- Cross-link this table from README and SPEC.

### Debt risk if deferred
- Repeated docs drift and contributor confusion.

---

## 6) Strengthen contributor quality gate documentation

### Why
`CLAUDE.md` defines a strong verification chain (`ruff`, `mypy`, `pytest`), but contributor behavior is more reliable when the same guidance is mirrored in user-facing docs used by all contributors.

### Improvement
- Add/refresh a concise "Contributing quality gate" section in `README.md` or `docs/`:
  - exact commands,
  - expected Python/Django versions,
  - failure triage order,
  - determinism expectations for SDL-related changes.

### Debt risk if deferred
- Inconsistent local verification and avoidable CI churn.

---

## Suggested execution order
1. Maturity signal alignment.
2. Quality gate docs normalization.
3. Typing suppression reduction.
4. Module decomposition.
5. Backend matrix hardening.
6. Terminology unification pass.

This order prioritizes low-risk/high-clarity wins first, then deeper internal debt paydown.
