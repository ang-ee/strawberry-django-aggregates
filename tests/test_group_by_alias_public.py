"""Public ``group_by_alias`` — freezes the group-key alias contract.

``group_by_alias`` is the single owner of the rule that maps a ``(field,
granularity)`` group-by pair to its canonical output alias. It is used
internally by the type emitter, the resolver, cursor pagination (§ 4.1),
and the having-echo (§ 4.3); consumers building their own grouped envelope
(e.g. a Hasura/NDC ``{ key, aggregate }`` shape) call it instead of
recomputing the ``_id`` / granularity suffix. These tests assert it is
importable from the package root and pin the four canonical outputs.
"""

from __future__ import annotations

import datetime
import decimal

import pytest

from strawberry_django_aggregates import (
    NumberGranularity,
    TimeGranularity,
    group_by_alias,
    group_by_enum_member,
    group_by_range_alias,
    make_group_by_spec,
    python_type_for_json,
)
from strawberry_django_aggregates.errors import JSONPathNotAllowed


def test_group_by_alias_is_public():
    """The promoted symbol is importable from the package root."""
    from strawberry_django_aggregates import group_by_alias as imported

    assert callable(imported)


def test_fk_axis_gets_id_suffix():
    """A to-one (FK) field with no granularity → ``<field>_id``."""
    from tests.models import Order

    fk = Order._meta.get_field("customer")
    assert fk.is_relation and fk.many_to_one  # guard the premise
    assert group_by_alias("customer", None, fk) == "customer_id"


def test_plain_field_passes_through():
    """A non-relation field with no granularity → the path verbatim."""
    from tests.models import Order

    plain = Order._meta.get_field("status")
    assert group_by_alias("status", None, plain) == "status"
    # And with no field supplied at all (defensive default).
    assert group_by_alias("status", None) == "status"


def test_time_bucket_suffixes_with_granularity():
    """A TIME-granularity bucket → ``<field>_<granularity>``."""
    assert (
        group_by_alias("created_at", TimeGranularity.MONTH)
        == "created_at_month"
    )


def test_number_bucket_suffixes_with_granularity():
    """A NUMBER-granularity bucket → ``<field>_<granularity>``."""
    assert (
        group_by_alias("created_at", NumberGranularity.DAY_OF_WEEK)
        == "created_at_day_of_week"
    )


def test_json_path_normalises_dot_to_double_underscore():
    """A dotted JSON path → its column-alias form (``.`` → ``__``).

    ``group_by_alias`` is the single owner of this rewrite (SPEC § 16);
    the type emitter, the resolver, cursor pagination, the having-echo,
    and the dense-fill spine all route JSON axes through it so the wire
    keys cannot drift. A model-field path never contains ``.``, so the
    rewrite is a no-op for the non-JSON cases above.
    """
    assert group_by_alias("metadata.region", None) == "metadata__region"


def test_json_path_applies_granularity_after_rewrite():
    """A dotted JSON path with granularity → rewrite, then suffix."""
    assert (
        group_by_alias("metadata.created_at", TimeGranularity.MONTH)
        == "metadata__created_at_month"
    )
    assert (
        group_by_alias(
            "metadata.created_at", NumberGranularity.DAY_OF_WEEK,
        )
        == "metadata__created_at_day_of_week"
    )


def test_group_by_enum_member_uses_the_canonical_path_rewrite():
    assert group_by_enum_member("metadata.region") == "METADATA__REGION"
    assert group_by_enum_member("customer__name") == "CUSTOMER__NAME"


def test_group_by_range_alias_uses_the_canonical_bucket_alias():
    assert (
        group_by_range_alias("metadata.created_at", TimeGranularity.WEEK)
        == "metadata__created_at_week_range"
    )
    with pytest.raises(ValueError, match="TimeGranularity"):
        group_by_range_alias(
            "created_at", NumberGranularity.DAY_OF_WEEK  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("str", str),
        ("int", int),
        ("float", float),
        ("Decimal", decimal.Decimal),
        ("bool", bool),
        ("date", datetime.date),
        ("datetime", datetime.datetime),
    ],
)
def test_python_type_for_json_owns_the_declared_token_vocabulary(
    token, expected
):
    assert python_type_for_json(token) is expected


def test_python_type_for_json_rejects_an_unknown_token():
    with pytest.raises(JSONPathNotAllowed, match="Unknown declared JSON type"):
        python_type_for_json("uuid")


def test_group_by_declaration_rejects_an_unknown_json_token():
    from tests.models import Order

    with pytest.raises(JSONPathNotAllowed, match="Unknown declared JSON type"):
        make_group_by_spec(
            Order,
            group_by_fields=["metadata.region"],
            json_paths={"metadata.region": "uuid"},
        )
