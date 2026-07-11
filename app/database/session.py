"""Database engine, session factory, and table initialization.

Uses SQLAlchemy Core/ORM. The same models work for both SQLite (default, zero
infra) and Postgres (production) — only ``DATABASE_URL`` changes.

``init_db()`` creates tables if they do not exist. It is safe to call at app
startup and from the scheduler entrypoint.
"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


# Engine: SQLite needs check_same_thread=False for FastAPI's threads;
# Postgres is fine with the same engine construction.
_connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, future=True, connect_args=_connect_args)

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
