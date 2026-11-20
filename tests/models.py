"""Models used by the strawberry-django-aggregates test suite.

Mirrors the SPEC examples (Customer / Order / OrderItem) so test code
reads like the spec. The o2m relation between Order and OrderItem is
deliberate — it lets us assert that the library refuses to aggregate
across `items__price` (would silently row-multiply).
"""

from __future__ import annotations

import enum

from django.db import models


class Customer(models.Model):
    name      = models.CharField(max_length=100)
    active    = models.BooleanField(default=True)
    joined_at = models.DateTimeField(null=True)

    class Meta:
        app_label = "tests"
        # Intrinsic ordering exercised by Stream 16's
        # ``respect_comodel_ordering`` flag. Other tests remain
        # robust to row order — they look rows up by FK id, not by
        # iteration order — so this addition is safe.
        ordering = ["name"]


class CustomerProfile(models.Model):
    """Nullable to-one keys, including a missing reverse relation."""

    customer = models.OneToOneField(
        Customer, on_delete=models.CASCADE, related_name="profile", null=True,
    )

    class Meta:
        app_label = "tests"


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


class ChoiceRecord(models.Model):
    """Blank and null are separate stored values on a choices field."""

    country = models.CharField(
        max_length=2,
        choices=[("CZ", "Czechia"), ("US", "United States")],
        blank=True,
        null=True,
        default="",
    )

    class Meta:
        app_label = "tests"


class StampField(models.DateTimeField):
    """A ``DateTimeField`` that only changes how its value is stamped."""


class AmountField(models.DecimalField):
    """A ``DecimalField`` with a rule of its own."""


class SealedTextField(models.TextField):
    """A ``TextField`` whose stored text no default operator may expose."""


class LoggedAtField(models.DateTimeField):
    """A ``DateTimeField`` that converts what it reads (``to_python``)."""

    def to_python(self, value):
        return super().to_python(value)


class Reading(models.Model):
    """Field subclasses and the type they aggregate as (SPEC § 5, § 7).

    ``stamped_at`` and ``amount`` subclass a temporal and a numeric field
    and return its values: they aggregate as ``DateTimeField`` and
    ``DecimalField``. ``logged_at`` converts its values, ``note``
    subclasses a text field and ``mirrored_at`` is generated: each keeps
    its class name.
    """

    taken_at    = models.DateTimeField()
    stamped_at  = StampField()
    amount      = AmountField(max_digits=10, decimal_places=2)
    note        = SealedTextField(blank=True, default="")
    logged_at   = LoggedAtField(null=True)
    mirrored_at = models.GeneratedField(
        expression=models.F("taken_at"),
        output_field=models.DateTimeField(),
        db_persist=True,
    )

    class Meta:
        app_label = "tests"


class Color(enum.Enum):
    RED = 1
    BLUE = 2


class Token:
    """An opaque public id wrapping a sequential integer."""

    def __init__(self, number: int) -> None:
        self.number = number

    def __str__(self) -> str:
        return f"tok_{self.number * 7919:x}"

    def __int__(self) -> int:
        return self.number

    def __hash__(self) -> int:
        return hash(self.number)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Token) and other.number == self.number


class EnumIntegerField(models.IntegerField):
    """An ``IntegerField`` whose Python value is an enum member."""

    def from_db_value(self, value, expression, connection):
        return None if value is None else Color(value)

    def get_prep_value(self, value):
        return value.value if isinstance(value, Color) else value


class OpaqueIdField(models.IntegerField):
    """An ``IntegerField`` whose Python value is an opaque token."""

    def from_db_value(self, value, expression, connection):
        return None if value is None else Token(value)

    def get_prep_value(self, value):
        return value.number if isinstance(value, Token) else value


class TokenUUIDField(models.UUIDField):
    """A ``UUIDField`` whose Python value is a prefixed token."""

    def from_db_value(self, value, expression, connection):
        return None if value is None else f"tok_{value}"


class UnitField(models.IntegerField):
    """An ``IntegerField`` that returns the integers it stores."""


class Ledger(models.Model):
    """Value-converting subclasses keep their name; a plain one inherits."""

    color = EnumIntegerField()
    ref   = OpaqueIdField()
    token = TokenUUIDField(null=True)
    units = UnitField(default=0)

    class Meta:
        app_label = "tests"


class ElapsedField(models.DurationField):
    """A ``DurationField`` subclass: no type is inherited from a duration."""


class Span(models.Model):
    """Duration columns that build as before: a subclass, a generated one."""

    start  = models.DateTimeField()
    end    = models.DateTimeField()
    lap    = ElapsedField(null=True)
    length = models.GeneratedField(
        expression=models.F("end") - models.F("start"),
        output_field=models.DurationField(),
        db_persist=True,
    )

    class Meta:
        app_label = "tests"


class Shelf(models.Model):
    name = models.CharField(max_length=20)

    class Meta:
        app_label = "tests"


class Volume(models.Model):
    """``order_with_respect_to`` adds Django's own ``_order`` column."""

    shelf = models.ForeignKey(Shelf, on_delete=models.CASCADE)
    title = models.CharField(max_length=20)
    pages = models.IntegerField(default=0)

    class Meta:
        app_label = "tests"
        order_with_respect_to = "shelf"


class TaggedEmailField(models.EmailField):
    pass


class TaggedURLField(models.URLField):
    pass


class TaggedSlugField(models.SlugField):
    pass


class Contact(models.Model):
    """Django's string fields keep their operators; their subclasses none."""

    email        = models.EmailField(default="")
    url          = models.URLField(default="")
    slug         = models.SlugField(default="")
    tagged_email = TaggedEmailField(default="")
    tagged_url   = TaggedURLField(default="")
    tagged_slug  = TaggedSlugField(default="")

    class Meta:
        app_label = "tests"
