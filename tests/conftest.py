import os
import socket

import pytest
from sqlalchemy import text

from app.memory.db import get_engine, init_db

# Local default; CI overrides via TEST_DATABASE_URL (its pgvector container only has the
# `postgres` role/port). The "test" guard makes it impossible to point the suite — which
# TRUNCATEs tables — at a non-test database.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://localhost/personal_agent_test")
assert "test" in TEST_DATABASE_URL, f"refusing to run tests against a non-test DB: {TEST_DATABASE_URL!r}"

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
    "purchase, emailsignal, processedemail, ingeststate, hostedconsult, websearch "
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
