"""Write, or read, the golden SDL of the library's models of Django built-ins.

``python tests/golden_sdl.py`` writes ``tests/golden/builtin_fields.graphql``
from the installed package; ``tests/test_builtin_sdl.py`` compares the
package's SDL with it. The file was written by running this script against
the ``v0.14.1`` release: a Django built-in field answers by its own type,
so the SDL of these models must not change. Regenerate it only for a
change this library means to make to them, and say so in the changelog.
"""

from __future__ import annotations

import re
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden" / "builtin_fields.graphql"
MODELS = ("BuiltinFields", "Contact", "Volume")
"""The library's test models whose fields are all Django built-ins."""
_KIND_BLOCKS = re.compile(
    r"^type \w+(?:GroupKey|Fields) \{.*?^\}\n", re.S | re.M,
)
"""The types a field's kind shows in: its group key and measure fields."""


def builtin_fields_sdl() -> str:
    """Return each model's group key and measure types under the defaults."""
    import strawberry

    from strawberry_django_aggregates import AggregateBuilder
    from tests import models

    parts = []
    for name in MODELS:
        built = AggregateBuilder(model=getattr(models, name)).build()
        namespace = {
            "__annotations__": {
                "agg": built.aggregate_type,
                "grouped": built.grouped_result_type,
            },
            "agg": built.aggregate_field,
            "grouped": built.group_by_field,
        }
        query = strawberry.type(type("Query", (), namespace))
        sdl = strawberry.Schema(query=query).as_str() + "\n"
        parts.append(f"# {name}\n" + "".join(_KIND_BLOCKS.findall(sdl)))
    return "".join(parts)


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.conftest import pytest_configure

    pytest_configure()
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(builtin_fields_sdl())
    print(f"wrote {GOLDEN}")
