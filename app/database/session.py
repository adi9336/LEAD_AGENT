"""Database engine, session factory, and table initialization.

Uses SQLAlchemy Core/ORM. The same models work for both SQLite (default, zero
infra) and Postgres (production) — only ``DATABASE_URL`` changes.

``init_db()`` creates tables if they do not exist. It is safe to call at app
startup and from the scheduler entrypoint.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def _resolve_db_url() -> str:
    """Pick the SQLite path.

    Vercel's runtime filesystem is read-only except ``/tmp``. A SQLite file
    under the repo (the default ``./lead_agent.db``) would fail to create on
    write, crashing ``init_db()``. On serverless we redirect to ``/tmp``.
    """
    url = settings.database_url
    if url.startswith("sqlite") and os.environ.get("VERCEL"):
        # ./lead_agent.db -> /tmp/lead_agent.db
        fname = url.rsplit("/", 1)[-1] or "lead_agent.db"
        return f"sqlite:////tmp/{fname}"
    return url


# Engine: SQLite needs check_same_thread=False for FastAPI's threads;
# Postgres is fine with the same engine construction.
_resolved = _resolve_db_url()
_connect_args = {"check_same_thread": False} if _resolved.startswith("sqlite") else {}
engine = create_engine(_resolved, future=True, connect_args=_connect_args)

# SessionLocal: a factory producing new sessions (one per request / task).
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_session() -> Session:
    """FastAPI dependency: yields a session and always closes it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (idempotent)."""
    # Import models so they register on Base.metadata before create_all.
    from app.database import models as _models  # noqa: F401
    Base.metadata.create_all(bind=engine)
