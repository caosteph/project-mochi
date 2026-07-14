import socket

import pytest
from sqlalchemy import text

from app.memory.db import get_engine, init_db

TEST_DATABASE_URL = "postgresql://localhost/personal_agent_test"

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


@pytest.fixture(autouse=True)
def clean_tables(engine):
    yield
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE fact, goal, task, reminder, event, messagelog, "
            "purchase, emailsignal, processedemail, ingeststate, hostedconsult "
            "RESTART IDENTITY CASCADE"
        ))
