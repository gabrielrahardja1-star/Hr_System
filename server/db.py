"""SQLAlchemy engine/session wiring for the Postgres database.

`pool_pre_ping` guards against the pooled connections going stale under a
long-lived uvicorn process. Schema changes go through Alembic (`alembic/`);
`init_db()` below is only a convenience for tests and a from-scratch local
bootstrap, not the production migration path.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from server.config import get_settings


def _make_engine():
    settings = get_settings()
    return create_engine(settings.database_url, future=True, pool_pre_ping=True)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create every table. Safe to call repeatedly. Tests/local bootstrap only —
    production schema changes go through `alembic upgrade head`."""
    from server import models  # noqa: F401  (registers mappers)

    models.Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session for scripts and the ingest path."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
