"""The guard that keeps the test suite off the production database.

Written after the suite wrote 48 test-fixture rows into Stephanie's real `messagelog`. The
mechanism was subtle and worth stating: tests patched `get_engine` *in the module under test*,
so when a handler moved from `app.channels.telegram` to `app.channels.telegram_commands`, the
patch followed it — but `_log_turn`/`_log_one` stayed behind and resolved the unpatched default,
which is whatever `.env` says. That is production.

Per-test patching is the wrong layer: forgetting it is silent and the blast radius is real data.
`conftest` now repoints the application's default database before any `app.*` import, and these
tests assert that guard directly rather than trusting it.
"""

from sqlmodel import Session, select

from app.config import settings
from app.memory.db import get_engine
from app.memory.models import MessageLog


def test_app_settings_point_at_a_scratch_database():
    assert "test" in settings.database_url, (
        f"tests must never resolve a non-test database; got {settings.database_url!r}"
    )


def test_a_bare_get_engine_call_is_safe_anywhere_in_app():
    """The failing case: code that calls `get_engine()` with no argument and was never
    monkeypatched. It must still land on the scratch DB."""
    assert "test" in str(get_engine().url)


def test_writing_through_an_unpatched_engine_stays_in_the_scratch_db():
    """End-to-end version of the bug: write via the default engine exactly as `_log_one` does,
    then confirm it landed somewhere the `clean_tables` fixture will truncate."""
    from app.memory import store

    with Session(get_engine()) as s:
        store.log_message(s, chat_id=1, role="user", text="isolation probe")
    with Session(get_engine()) as s:
        rows = [m for m in s.exec(select(MessageLog)) if m.text == "isolation probe"]
    assert rows, "the write should be visible in the scratch DB"
    assert "test" in str(get_engine().url)


def test_channel_turn_logging_does_not_escape_to_production(channel):
    """Directly exercises the path that leaked: TelegramChannel._log_one, unpatched."""
    import asyncio

    asyncio.run(channel._log_turn(1, "user side", "assistant side"))
    with Session(get_engine()) as s:
        texts = {m.text for m in s.exec(select(MessageLog))}
    assert {"user side", "assistant side"} <= texts
    assert "test" in str(get_engine().url)


def test_no_orm_object_outlives_its_session():
    """Runs scripts/audit_session_scope.py over app/ as part of the normal suite.

    This bug class shipped three times and unit tests structurally cannot catch it — a mocked
    session never expires its instances — so the static check is the guard, and it belongs in
    CI rather than in a script someone remembers to run. Validated against the pre-fix tree:
    it flags all three historical instances.
    """
    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from scripts.audit_session_scope import audit

    findings = audit(pathlib.Path("app"))
    assert not findings, "ORM object(s) used after their session closed:\n" + "\n".join(
        f"  {p}:{line} {expr} — {why}" for p, line, expr, why in findings
    )
