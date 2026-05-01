"""Pytest configuration for strawberry-django-aggregates tests.

Provides a minimal Django setup so tests can use ORM querysets and
strawberry schemas without booting a full project.
"""

from __future__ import annotations

import django
from django.conf import settings


def pytest_configure() -> None:
    if settings.configured:
        return
    settings.configure(
        DEBUG=True,
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME":   ":memory:",
            },
        },
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "tests",
        ],
        USE_TZ=True,
        TIME_ZONE="UTC",
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    )
    django.setup()
