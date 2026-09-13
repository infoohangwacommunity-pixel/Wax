"""Integration tests for the HTTP health surface (Phase A)."""

from __future__ import annotations

from httpx import AsyncClient


class TestHealthEndpoints:
    """Health endpoints are the runtime's liveness and readiness probes."""

    async def test_root(self, client: AsyncClient) -> None:
        r = await client.get("/")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "WAX"
        assert "version" in body
        assert "constitution" in body
        assert "infrastructure, not intelligence" in body["constitution"].lower()

    async def test_healthz(self, client: AsyncClient) -> None:
        r = await client.get("/healthz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "version" in body

    async def test_readyz(self, client: AsyncClient) -> None:
        r = await client.get("/readyz")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "checks" in body
        assert body["checks"]  # non-empty

    async def test_unknown_route_returns_404(self, client: AsyncClient) -> None:
        r = await client.get("/this-does-not-exist")
        assert r.status_code == 404

    async def test_openapi_in_development(self, client: AsyncClient) -> None:
        # The test app is created with development settings by default,
        # so the OpenAPI schema should be available.
        r = await client.get("/openapi.json")
        assert r.status_code == 200
        schema = r.json()
        assert "/healthz" in schema["paths"]
        assert "/readyz" in schema["paths"]
        assert "/" in schema["paths"]
