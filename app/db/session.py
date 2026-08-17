"""Database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from pgvector.psycopg import register_vector
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


def normalize_database_url(url: str) -> str:
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


@lru_cache
def get_engine() -> Engine:
    engine = create_engine(normalize_database_url(get_settings().database_url))

    @event.listens_for(engine, "connect")
    def _register_vector(dbapi_connection, _connection_record) -> None:
        register_vector(dbapi_connection)

    return engine


def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, autocommit=False)


@contextmanager
def db_session() -> Iterator[Session]:
    """Yield a SQLAlchemy session bound to the configured database."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()
