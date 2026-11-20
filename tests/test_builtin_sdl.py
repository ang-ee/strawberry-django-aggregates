"""A Django built-in field gives the SDL it always gave (SPEC § 5).

``tests/golden/builtin_fields.graphql`` was written by
``tests/golden_sdl.py`` against the ``v0.14.1`` release.
"""

from __future__ import annotations

import difflib

from tests.golden_sdl import GOLDEN, builtin_fields_sdl


def test_built_in_fields_give_the_golden_sdl(db):
    expected, actual = GOLDEN.read_text(), builtin_fields_sdl()

    assert actual == expected, "".join(difflib.unified_diff(
        expected.splitlines(keepends=True), actual.splitlines(keepends=True),
        "golden", "package",
    ))
