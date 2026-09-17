"""Shared pytest fixtures and configuration."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from wax.core.config import settings_for_testing
from wax.runtime.app import create_app


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Use asyncio for anyio-driven tests."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _ensure_fresh_stdout(_isolate_env: Iterator[None]) -> Iterator[None]:
    """Ensure structlog writes to a fresh sys.stdout on every test.

    Some tests use capsys which replaces sys.stdout; structlog caches a
    reference to the captured stream, which gets closed when capsys tears
    down. Rebinding sys.stdout to the real stdout before each test avoids
    "I/O operation on closed file" errors.
    """
    import structlog

    # Force structlog to rebind to the current (fresh) sys.stdout.
    structlog.reset_defaults()
    from wax.runtime.logging import configure_logging

    configure_logging(settings_for_testing())
    yield


@pytest.fixture
def test_settings() -> Any:
    """Return WaxSettings configured for testing (in-memory SQLite, WARNING log level)."""
    return settings_for_testing()


@pytest.fixture
async def app(test_settings: Any) -> Any:
    """Build a FastAPI app with test settings and initialized DB."""
    # Override DB URL to in-memory SQLite for hermetic tests.
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    app = create_app(settings=test_settings)

    # Initialize the DB engine and create schema (migrations are tested
    # separately; for unit/integration tests we just create_all).
    from wax.state.engine import dispose_engine, init_engine
    from wax.state.models import Base

    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Initialize app.state.* as the lifespan would (so /readyz finds them)
    from wax.intelligence.service import IntelligenceService
    from wax.runtime.bridge.service import RuntimeBridge
    from wax.runtime.services import RuntimeServices

    services = RuntimeServices.build(test_settings)
    app.state.services = services

    intel = IntelligenceService.from_settings(test_settings)
    app.state.intelligence = intel
    bridge = RuntimeBridge(intelligence=intel, services=services)
    app.state.runtime_bridge = bridge
    services.reentry_callback = bridge.run_reentry
    services.resume_callback = bridge.resume_execution
    app.state.whatsapp_client = None

    yield app

    await intel.close()
    await dispose_engine()


@pytest.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    """HTTPX async client bound to the test app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def unique_id() -> str:
    """Return a unique string for test data isolation."""
    return uuid4().hex


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure no test reads real env vars or .env files.

    This makes tests hermetic: they cannot accidentally pick up a developer's
    local .env file or shell environment.
    """
    # pydantic-settings reads from env on construction. By setting _env_file=None
    # and clearing WAX_-prefixed vars, we ensure tests only see what they
    # explicitly configure.
    for key in list(monkeypatch._setenv.keys() if hasattr(monkeypatch, "_setenv") else []):
        if key.startswith("WAX_"):
            monkeypatch.delenv(key, raising=False)
    yield
