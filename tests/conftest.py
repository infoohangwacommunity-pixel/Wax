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


@pytest.fixture
def test_settings() -> Any:
    """Return WaxSettings configured for testing (in-memory SQLite, WARNING log level)."""
    return settings_for_testing()


@pytest.fixture
async def app(test_settings: Any) -> Any:
    """Build a FastAPI app with test settings."""
    return create_app(settings=test_settings)


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
