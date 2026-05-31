"""Models used by the strawberry-django-aggregates test suite.

Mirrors the SPEC examples (Customer / Order / OrderItem) so test code
reads like the spec. The o2m relation between Order and OrderItem is
deliberate — it lets us assert that the library refuses to aggregate
across `items__price` (would silently row-multiply).
"""

from __future__ import annotations

from django.db import models


class Customer(models.Model):
    name   = models.CharField(max_length=100)
    active = models.BooleanField(default=True)

    class Meta:
        app_label = "tests"
        # Intrinsic ordering exercised by Stream 16's
        # ``respect_comodel_ordering`` flag. Other tests remain
        # robust to row order — they look rows up by FK id, not by
        # iteration order — so this addition is safe.
        ordering = ["name"]


class Order(models.Model):
    STATUS_CHOICES = [
        ("draft",     "Draft"),
        ("paid",      "Paid"),
        ("cancelled", "Cancelled"),
    ]

    customer    = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="orders",
    )
    status      = models.CharField(max_length=16, choices=STATUS_CHOICES)
    total       = models.DecimalField(
        max_digits=10, decimal_places=2, null=True,
    )
    quantity    = models.IntegerField(default=1)
    is_priority = models.BooleanField(default=False)
    created_at  = models.DateTimeField()
    # Stream 17 — JSONB-stored properties for analytics. Tests pin the
    # group_by + aggregation behaviour over typed JSON paths
    # (``metadata.region``, ``metadata.amount``, ``metadata.created_at_iso``).
    metadata    = models.JSONField(default=dict, blank=True)

    class Meta:
        app_label = "tests"


class OrderItem(models.Model):
    order  = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="items",
    )
    sku    = models.CharField(max_length=32)
    price  = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        app_label = "tests"


class Task(models.Model):
    """Exercises choices-backed group-by enum emission edge cases.

    - ``priority`` uses plain integer ``choices`` — the emitted enum
      member names cannot start with a digit, so they are derived from
      the labels (``1 / "Low"`` -> ``LOW``).
    - ``effort`` is a plain ``IntegerField`` with no choices — it must
      keep its ``Int`` scalar on the group key.

    The django-choices-field ``choices_enum`` (name != value) path is
    exercised in ``test_choices_group_key`` by attaching a ``choices_enum``
    attribute to a plain field — the library reads it via ``getattr`` and
    does NOT depend on ``django-choices-field``.
    """

    PRIORITY_CHOICES = [
        (1, "Low"),
        (2, "Medium"),
        (3, "High"),
    ]

    priority = models.IntegerField(choices=PRIORITY_CHOICES, default=1)
    effort   = models.IntegerField(default=0)

    class Meta:
        app_label = "tests"
