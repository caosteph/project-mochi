import os
import socket

import pytest
from sqlalchemy import text

# Local default; CI overrides via TEST_DATABASE_URL (its pgvector container only has the
# `postgres` role/port). The "test" guard makes it impossible to point the suite — which
# TRUNCATEs tables — at a non-test database.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost/personal_agent_test")
assert "test" in TEST_DATABASE_URL, f"refusing to run tests against a non-test DB: {TEST_DATABASE_URL!r}"

# Point the APPLICATION's default database at the scratch DB, before any `app.*` import
# resolves `settings.database_url` from .env. Environment beats .env in pydantic-settings, so
# this makes a bare `get_engine()` — anywhere in app/ — safe.
#
# This is a hard guard, not tidiness. Tests used to rely on each test monkeypatching
# `get_engine` in the module under test; when the /ask handlers moved from `telegram` to
# `telegram_commands`, the patch moved with them but `_log_turn`/`_log_one` stayed behind in
# `telegram`, resolving the UNPATCHED default — and wrote 48 test-fixture rows into
# Stephanie's real message log. Per-test patching is the wrong layer for this: forgetting it is
# silent, and the blast radius is production data.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app.config import settings  # noqa: E402
from app.memory.db import get_engine, init_db  # noqa: E402

# Belt and braces: if something imported app.config before this file (a plugin, an -p option),
# the env var above arrived too late — so correct the resolved value too, and fail loudly
# rather than silently writing somewhere real.
settings.database_url = TEST_DATABASE_URL
assert "test" in settings.database_url, "app settings must point at the scratch DB during tests"

_ALLOWED_HOSTS = {"127.0.0.1", "::1", "localhost"}
_orig_connect = socket.socket.connect


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host not in _ALLOWED_HOSTS:
        raise RuntimeError(f"Blocked outbound connection to {host!r} during tests")
    return _orig_connect(self, address)


@pytest.fixture(autouse=True)
def block_non_local_network(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    from app.agent import rate_limit

    rate_limit.reset()
    yield
    rate_limit.reset()


@pytest.fixture(scope="session")
def engine():
    eng = get_engine(TEST_DATABASE_URL)
    init_db(eng)
    return eng


_TRUNCATE = text(
    "TRUNCATE fact, goal, task, reminder, event, messagelog, "
    "purchase, emailsignal, processedemail, ingeststate, hostedconsult, websearch, retiredtopic "
    "RESTART IDENTITY CASCADE"
)


@pytest.fixture(autouse=True)
def clean_tables(engine):
    """Truncate before AND after.

    After alone isn't enough: the scratch DB is shared with `scripts/verify_*.py`, which leave
    rows behind, so the first test of a run inherited them. That surfaced as a test asserting
    "nothing has been sent externally" failing against a HostedConsult row written by
    verify_phase4b minutes earlier — a real isolation gap, not a flake.
    """
    with engine.begin() as conn:
        conn.execute(_TRUNCATE)
    yield
    with engine.begin() as conn:
        conn.execute(_TRUNCATE)


# --- shared scaffolding (see tests/support/) --------------------------------
# These replace the fake bot reimplemented 8×, the fake callback query 2×, the fake model 2×, the
# TelegramChannel.__new__ bypass ~20×, and the scattered row-seeding helpers. Test files request
# these fixtures instead of hand-rolling doubles, so the scaffolding lives in exactly one place.


@pytest.fixture
def fake_bot():
    """A recording stand-in for the Telegram Bot (see tests.support.FakeBot)."""
    from tests.support import FakeBot

    return FakeBot()


@pytest.fixture
def ctx(fake_bot):
    """The python-telegram-bot context handlers receive — just needs `.bot`."""
    from types import SimpleNamespace

    return SimpleNamespace(bot=fake_bot)


@pytest.fixture
def channel(monkeypatch):
    """A TelegramChannel test double: built via __new__ (skips build_agent), authorized by default,
    with an empty /ask thread store. Override `_authorized`/`agent`/`_log_one` per test as needed."""
    from app.channels import telegram

    ch = telegram.TelegramChannel.__new__(telegram.TelegramChannel)
    ch._ask_threads = {}
    monkeypatch.setattr(ch, "_authorized", lambda _update: True)
    return ch


@pytest.fixture
def seed(engine):
    """Row factories bound to the test engine: `seed.reminder("dentist", days=2)` returns the row.

    Each call opens its own session on the shared test engine, so tests seed data without touching
    a Session. Wraps tests.support.factories."""
    from sqlmodel import Session

    from tests.support import factories

    class _Seed:
        def reminder(self, text, **kw):
            with Session(engine) as s:
                return factories.make_reminder(s, text, **kw)

        def signal(self, **kw):
            with Session(engine) as s:
                return factories.make_signal(s, **kw)

        def fact(self, text, **kw):
            with Session(engine) as s:
                return factories.make_fact(s, text, **kw)

        def task(self, text, **kw):
            with Session(engine) as s:
                return factories.make_task(s, text, **kw)

    return _Seed()
