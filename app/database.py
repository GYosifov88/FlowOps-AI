"""Database setup: declarative base, engine factory, sync sessions.

Sessions are SYNC and routes that use them are plain `def` (FastAPI runs those in a
threadpool). Do not introduce the async engine.
"""
from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import DateTime, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import TypeDecorator

from app.config import get_settings
from app.timeutil import to_naive_utc


class Base(DeclarativeBase):
    pass


class NaiveUTCDateTime(TypeDecorator):
    """DateTime stored as naive UTC.

    An aware value is converted to UTC and its tzinfo stripped on the way in; a naive
    value is assumed to already be UTC. Values come back naive. We deliberately do not
    use DateTime(timezone=True): SQLite does not preserve tzinfo, so it would only look
    safe.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        return to_naive_utc(value)


def _set_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    # The only SQLite-specific SQL in the project; it is isolated here and only
    # registered for SQLite engines.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_engine(url: str) -> Engine:
    engine = create_engine(url)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


engine = create_db_engine(get_settings().database_url)
SessionLocal = sessionmaker(bind=engine)


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request. Never commits; services commit."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(bind: Engine | None = None) -> None:
    """Create all tables (no migrations until Phase 2)."""
    import app.models  # noqa: F401  (registers the tables on Base.metadata)

    Base.metadata.create_all(bind=bind or engine)
