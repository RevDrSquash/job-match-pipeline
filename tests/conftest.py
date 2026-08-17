"""Shared fixtures for local TaskQueue HTTP tests and DB integration tests."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import httpx
import pytest
import uvicorn
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import get_engine, normalize_database_url
from app.handlers import clear_received


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _database_available() -> bool:
    engine = create_engine(normalize_database_url(get_settings().database_url))
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False
    finally:
        engine.dispose()


requires_db = pytest.mark.skipif(
    not _database_available(),
    reason="Postgres not reachable (start with: docker compose up db -d)",
)


@pytest.fixture(scope="session")
def apply_migrations() -> None:
    from alembic.config import Config

    from alembic import command

    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")


@pytest.fixture
def db_session(apply_migrations: None) -> Iterator[Session]:
    engine = get_engine()
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def local_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Boot uvicorn with QUEUE_IMPL=local and yield the base URL."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    monkeypatch.setenv("QUEUE_IMPL", "local")
    monkeypatch.setenv("LOCAL_QUEUE_BASE_URL", base_url)
    monkeypatch.setenv("ENABLE_DEBUG_CAPTURE", "true")
    get_settings.cache_clear()

    # Import after env is set so module-level defaults stay unused by create_app().
    from app.config import Settings
    from app.main import create_app
    from app.queue import get_task_queue

    settings = Settings()
    queue = get_task_queue(settings)
    application = create_app(settings=settings, queue=queue)

    clear_received()

    config = uvicorn.Config(
        application,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 10
    while time.time() < deadline:
        if server.started:
            try:
                response = httpx.get(f"{base_url}/health", timeout=0.5)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
        time.sleep(0.05)
    else:
        server.should_exit = True
        raise RuntimeError("uvicorn failed to start")

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        clear_received()
        get_settings.cache_clear()
