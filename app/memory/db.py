"""Postgres engine + idempotent schema setup for the memory tables. Table
creation and index creation are split: SQLModel's create_all() for tables,
raw idempotent SQL for the HNSW/GIN indexes — more transparent than getting
SQLAlchemy's declarative index kwargs exactly right, same idempotent-on-every-
startup spirit as PostgresSaver.setup() in agent/graph.py.
"""

from sqlalchemy import Engine, create_engine, text
from sqlmodel import SQLModel

from app.config import settings

# Import the models module for its side effect: registering every table on
# SQLModel.metadata. Without this, init_db() below depends on some *other*
# module having imported the models first — which is true in the running app
# by luck of the import chain, but makes init_db() silently create zero tables
# (then crash on the index DDL) when called in isolation. Registering here
# makes init_db self-sufficient. No circular import: models imports nothing
# from this package.
from app.memory import models  # noqa: F401

_engine: Engine | None = None


def _sqlalchemy_url(database_url: str) -> str:
    # DATABASE_URL is written in the plain postgresql:// form shared with the
    # raw-psycopg checkpointer connection in graph.py; SQLAlchemy needs the
    # driver spelled out explicitly.
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return database_url


def get_engine(database_url: str | None = None) -> Engine:
    """Lazy singleton by default; tests pass an explicit database_url (a
    scratch personal_agent_test DB) to bypass the cached default engine."""
    global _engine
    if database_url is not None:
        return create_engine(_sqlalchemy_url(database_url))
    if _engine is None:
        _engine = create_engine(_sqlalchemy_url(settings.database_url))
    return _engine


def init_db(engine: Engine | None = None) -> None:
    """Idempotent: safe to call on every app startup."""
    engine = engine or get_engine()
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        # HNSW, not IVFFlat: IVFFlat's `lists` parameter needs representative
        # data present at creation time to be effective, which doesn't fit an
        # idempotent-on-startup pattern against a possibly-empty table.
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_fact_embedding_hnsw "
            "ON fact USING hnsw (embedding vector_cosine_ops)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_fact_text_tsv "
            "ON fact USING gin (to_tsvector('english', text))"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_event_embedding_hnsw "
            "ON event USING hnsw (embedding vector_cosine_ops)"
        ))
        # Phase 3A: the `reminder` table already existed (Phase 1), and
        # create_all() does NOT alter existing tables — so its new columns must
        # be added explicitly and idempotently, or the first insert crashes with
        # "column does not exist". (Purchase is a new table, handled by create_all.)
        for col_def in (
            "recurrence VARCHAR",
            "kind VARCHAR DEFAULT 'generic'",
            "purchase_id INTEGER REFERENCES purchase(id)",
            "calendar_event_id VARCHAR",
            "sent_at TIMESTAMP WITH TIME ZONE",
        ):
            conn.execute(text(f"ALTER TABLE reminder ADD COLUMN IF NOT EXISTS {col_def}"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reminder_kind ON reminder (kind)"))
