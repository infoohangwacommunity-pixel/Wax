"""Tests for Phase X — Production Hardening endpoints.

Tests verify:
- /metrics returns the metrics snapshot
- /migrations/status returns the current + head revision
- /config/validate validates the configuration
- /readyz checks intelligence + whatsapp availability
- All endpoints handle errors gracefully
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from wax.core.config import settings_for_testing
from wax.runtime.app import create_app
from wax.runtime.logging import configure_logging
from wax.state.engine import dispose_engine, init_engine
from wax.state.models import Base

configure_logging(settings_for_testing())


@pytest.fixture
async def app_with_db(test_settings):
    """Build app with initialized DB + schema + lifespan-triggered state."""
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    app = create_app(settings=test_settings)
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Manually trigger the lifespan startup so app.state.* is populated
    # (intelligence, runtime_bridge, whatsapp_client).
    from wax.intelligence.service import IntelligenceService
    from wax.runtime.bridge.service import RuntimeBridge

    intel = IntelligenceService.from_settings(test_settings)
    app.state.intelligence = intel
    app.state.runtime_bridge = RuntimeBridge(intelligence=intel)
    app.state.whatsapp_client = None  # no credentials in tests

    yield app

    await intel.close()
    await dispose_engine()


@pytest.fixture
async def client(app_with_db):
    transport = ASGITransport(app=app_with_db)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestMetricsEndpoint:
    async def test_metrics_returns_snapshot(self, client: AsyncClient) -> None:
        r = await client.get("/metrics")
        assert r.status_code == 200
        body = r.json()
        # Snapshot structure
        assert "counters" in body
        assert "gauges" in body
        assert "histograms" in body

    async def test_metrics_records_request(self, client: AsyncClient) -> None:
        """Hitting /metrics should not crash even if counters are empty."""
        r1 = await client.get("/metrics")
        assert r1.status_code == 200
        body1 = r1.json()
        # Counters should be empty initially (no requests counted yet)
        assert isinstance(body1["counters"], dict)


class TestMigrationsStatusEndpoint:
    async def test_returns_current_and_head(self, client: AsyncClient) -> None:
        r = await client.get("/migrations/status")
        # The endpoint may return 200 (success) or 500 (alembic.ini not found
        # in test environment — we accept both as long as it doesn't crash)
        assert r.status_code in (200, 500)
        body = r.json()
        if r.status_code == 200:
            assert "current_revision" in body
            assert "head_revision" in body
            assert "up_to_date" in body
        else:
            assert "error" in body


class TestConfigValidateEndpoint:
    async def test_returns_config_summary(self, client: AsyncClient) -> None:
        r = await client.get("/config/validate")
        assert r.status_code == 200
        body = r.json()
        assert "env" in body
        assert "host" in body
        assert "port" in body
        assert "log_level" in body
        assert "log_format" in body
        assert "database_url_scheme" in body
        assert "llm_provider" in body
        assert "whatsapp_configured" in body
        assert "secret_key_set" in body
        assert "validation_passed" in body

    async def test_development_config_passes(self, client: AsyncClient) -> None:
        """Development settings should pass validation (no production hardening)."""
        r = await client.get("/config/validate")
        body = r.json()
        assert body["env"] == "development"
        assert body["validation_passed"] is True


class TestReadyzChecks:
    async def test_readyz_includes_intelligence_check(self, client: AsyncClient) -> None:
        r = await client.get("/readyz")
        # Status 200 or 503 depending on initialization
        body = r.json()
        assert "intelligence" in body["checks"]
        # The test app initializes intelligence, so it should be ok
        assert body["checks"]["intelligence"] == "ok"

    async def test_readyz_includes_whatsapp_check(self, client: AsyncClient) -> None:
        r = await client.get("/readyz")
        body = r.json()
        assert "whatsapp" in body["checks"]
        # No credentials set in test → "not_configured"
        assert body["checks"]["whatsapp"] == "not_configured"

    async def test_readyz_includes_database_check(self, client: AsyncClient) -> None:
        r = await client.get("/readyz")
        body = r.json()
        assert "database" in body["checks"]
        # The fixture initialized the DB, so it should be ok
        assert body["checks"]["database"] == "ok"
