"""Shared test scaffolding (doubles + row factories).

Imported as a PEP 420 namespace subpackage under `tests/` (no `__init__.py` on `tests/` itself,
same as `scripts/`), which works because `pythonpath = ["."]`. Most tests reach these through the
`conftest.py` fixtures (`channel`, `fake_bot`, `ctx`, `seed`) rather than importing directly.
"""

from tests.support.fakes import (
    FakeBot,
    FakeMessage,
    FakeModel,
    FakeQuery,
    SentMessage,
    inline_markup,
    make_update,
)

__all__ = [
    "FakeBot",
    "FakeMessage",
    "FakeModel",
    "FakeQuery",
    "SentMessage",
    "inline_markup",
    "make_update",
]
