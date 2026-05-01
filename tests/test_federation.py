"""Apollo Federation v2 directive emission — SPEC § 18.

The library's federation surface is opt-in via ``AggregateBuilder(
enable_federation=True)``. v1.0 emits ``@external`` on the foreign-key
``<name>_id`` fields of ``<Model>GroupKey`` and switches every emitted
type's decorator to its ``strawberry.federation.*`` counterpart. The
consumer is expected to construct the schema with
:class:`strawberry.federation.Schema` (NOT plain ``strawberry.Schema``)
so the directives print and the federation protocol's ``_service``
introspection is wired.

We don't import ``from __future__ import annotations`` so strawberry can
resolve the dynamic type references on the ``Query`` class to live class
objects (mirrors the convention in ``test_builder_integration.py``).
"""

import strawberry
import strawberry.federation

from strawberry_django_aggregates import AggregateBuilder


def _build_schema(enable_federation: bool):
    """Return ``schema.as_str()`` for an Order aggregate schema with
    the federation flag set as requested.

    Uses :class:`strawberry.federation.Schema` so directives print when
    enabled. With ``enable_federation=False`` we still return federation
    schema SDL — but no federation directives are emitted on our
    aggregate types because the type-generators stayed on
    ``strawberry.type``.
    """
    from tests.models import Order

    built = AggregateBuilder(
        model=Order,
        aggregate_fields=["total", "quantity"],
        group_by_fields=["customer", "status", "created_at"],
        enable_federation=enable_federation,
    ).build()

    @strawberry.type
    class Query:
        order_aggregate: built.aggregate_type = built.aggregate_field
        orders_group_by: built.grouped_result_type = built.group_by_field

    schema = strawberry.federation.Schema(query=Query)
    return schema.as_str()


def test_federation_off_emits_no_directives(db):
    """Default flag — no ``@external`` and no ``@key`` on our types.

    The federation Schema still imports its bookkeeping
    (``_service`` etc.) but our aggregate types are vanilla
    Strawberry — they carry no federation directives.
    """
    sdl = _build_schema(enable_federation=False)
    # Locate the ``OrderGroupKey`` block and assert it has no directives.
    block = _block(sdl, "type OrderGroupKey")
    assert "@external" not in block, (
        f"Default-off path emitted @external:\n{block}"
    )
    assert "@key" not in block, f"Default-off path emitted @key:\n{block}"
    # The aggregate container also should not carry federation
    # directives in the off path.
    agg_block = _block(sdl, "type OrderAggregate")
    assert "@external" not in agg_block
    assert "@key" not in agg_block


def test_federation_on_emits_external_on_fk_groupkey(db):
    """Flag on — ``@external`` appears on the FK ``customerId`` field."""
    sdl = _build_schema(enable_federation=True)
    block = _block(sdl, "type OrderGroupKey")
    # ``customer`` FK on Order surfaces as ``customerId`` per SPEC § 4
    # and gets ``@external`` per SPEC § 18.
    assert "customerId:" in block, f"customerId field missing:\n{block}"
    customer_line = next(
        line
        for line in block.splitlines()
        if line.lstrip().startswith("customerId:")
    )
    assert "@external" in customer_line, (
        f"customerId did not get @external:\n{customer_line}"
    )
    # Non-FK fields stay un-decorated.
    status_line = next(
        line
        for line in block.splitlines()
        if line.lstrip().startswith("status:")
    )
    assert "@external" not in status_line, (
        f"status got an unexpected @external:\n{status_line}"
    )


def test_federation_on_no_key_on_aggregate_in_v1_0(db):
    """SPEC § 18: ``@key`` on the aggregate container is deferred to
    v1.1. v1.0 must NOT emit one — that decision belongs to the
    consumer until the keying convention stabilizes.
    """
    sdl = _build_schema(enable_federation=True)
    agg_block = _block(sdl, "type OrderAggregate")
    assert "@key" not in agg_block, (
        f"v1.0 must not emit @key on the aggregate type:\n{agg_block}"
    )
    grouped_block = _block(sdl, "type OrderGrouped")
    assert "@key" not in grouped_block, (
        f"v1.0 must not emit @key on the grouped type:\n{grouped_block}"
    )


def test_federation_sdl_is_byte_identical_across_runs(db):
    """Determinism (Critical Rule 2) — generate twice, byte-diff."""
    sdl_a = _build_schema(enable_federation=True)
    sdl_b = _build_schema(enable_federation=True)
    assert sdl_a == sdl_b, (
        "Federation-enabled SDL non-deterministic across runs."
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block(sdl: str, header: str) -> str:
    """Return the brace-delimited block whose opening line starts with
    ``header`` (e.g. ``"type OrderGroupKey"``).

    Returns the empty string if the header isn't found — the caller's
    assertions then surface the failure with a clear message.
    """
    lines = sdl.splitlines()
    in_block = False
    out: list[str] = []
    depth = 0
    for line in lines:
        if not in_block:
            if line.lstrip().startswith(header):
                in_block = True
                out.append(line)
                depth = line.count("{") - line.count("}")
                if depth == 0 and "{" in line:
                    break
                continue
        else:
            out.append(line)
            depth += line.count("{") - line.count("}")
            if depth <= 0:
                break
    return "\n".join(out)
