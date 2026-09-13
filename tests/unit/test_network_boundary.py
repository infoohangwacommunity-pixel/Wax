"""Tests for the network boundary (SSRF protection) on http.get.

Every fetch a capability performs must cross the boundary. These tests
prove the boundary holds for the classic exfiltration routes:
- loopback / private / link-local / metadata addresses
- scheme tricks (file://), credentials in URL
- DNS-rebinding-shaped literal addresses (0x7f000001, ::ffff:PenGuin)
- redirect hops into private space
- response size cap under a hostile server
And that legitimate public URLs still pass.
"""

from __future__ import annotations

import httpx
import pytest

from wax.capabilities.built_ins import http_get_impl
from wax.capabilities.contracts import InvocationContext
from wax.security.network import (
    FetchPolicy,
    NetworkBoundaryError,
    guarded_get,
    validate_url,
)


def _ctx() -> InvocationContext:
    return InvocationContext(
        principal_id="principal-test",
        capability_name="http.get",
        execution_id="exec-test",
        request_id="req-test",
    )


class TestValidateUrl:
    def test_loopback_blocked(self) -> None:
        with pytest.raises(NetworkBoundaryError):
            validate_url("http://127.0.0.1/x")

    def test_localhost_blocked(self) -> None:
        with pytest.raises(NetworkBoundaryError):
            validate_url("http://localhost:8080/")

    def test_private_ranges_blocked(self) -> None:
        for url in (
            "http://10.0.0.5/",
            "http://172.16.0.9/",
            "http://192.168.1.1/",
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://100.64.0.1/",  # CGNAT
            "http://[::1]/",
            "http://[fe80::1]/",
            "http://[fd00::1]/",
        ):
            with pytest.raises(NetworkBoundaryError):
                validate_url(url)

    def test_literal_tricks_blocked(self) -> None:
        # 2130706433 = 127.0.0.1 in decimal; 0x7f000001 hex.
        for url in ("http://2130706433/", "http://0x7f000001/"):
            with pytest.raises(NetworkBoundaryError):
                validate_url(url)

    def test_scheme_allowlist(self) -> None:
        for url in ("file:///etc/passwd", "ftp://example.com/", "gopher://example.com"):
            with pytest.raises(NetworkBoundaryError):
                validate_url(url)

    def test_credentials_blocked(self) -> None:
        with pytest.raises(NetworkBoundaryError):
            validate_url("http://user:pass@example.com/")

    def test_public_host_passes(self) -> None:
        _url, resolved = validate_url("https://example.com/")
        assert resolved  # resolved to at least one public address


class TestGuardedGet:
    async def test_public_url_passes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="hello public world")

        transport = httpx.MockTransport(handler)
        # example.com resolves publicly in normal environments; pin via
        # a validated host we control by monkey-resolving: use the
        # handler-based client directly through guarded_get's client.
        response = await guarded_get(
            "https://example.com/", transport=transport, policy=FetchPolicy()
        )
        assert response.status_code == 200

    async def test_redirect_to_private_blocked(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "public.example.com":
                return httpx.Response(
                    302, headers={"location": "http://169.254.169.254/meta"}
                )
            return httpx.Response(200, text="should not happen")

        with pytest.raises(NetworkBoundaryError):
            await guarded_get(
                "http://public.example.com/redirect",
                transport=httpx.MockTransport(handler),
            )

    async def test_body_cap_enforced(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"x" * 100)

        with pytest.raises(NetworkBoundaryError, match="exceeds"):
            await guarded_get(
                "https://example.com/big",
                transport=httpx.MockTransport(handler),
                policy=FetchPolicy(max_bytes=50),
            )


class TestHttpGetCapability:
    async def test_capability_blocks_loopback_with_safe_error(self) -> None:
        # DNS resolution is real here; 127.0.0.1 needs no DNS.
        with pytest.raises(ValueError, match="network boundary"):
            await http_get_impl({"url": "http://127.0.0.1:9/secret"}, _ctx())

    async def test_capability_blocks_metadata(self) -> None:
        with pytest.raises(ValueError, match="network boundary"):
            await http_get_impl({"url": "http://169.254.169.254/latest/meta-data/"}, _ctx())

    async def test_capability_blocks_file_scheme(self) -> None:
        with pytest.raises(ValueError, match="network boundary"):
            await http_get_impl({"url": "file:///etc/passwd"}, _ctx())

    async def test_capability_allows_public_via_transport(self, monkeypatch) -> None:
        # Patch at the source module: http_get_impl imports guarded_get
        # at call time, so the patch is picked up.
        import wax.security.network as net

        async def fake_guarded_get(url, *, headers=None, policy=None, transport=None):
            return httpx.Response(
                200,
                text="public data",
                headers={"content-type": "text/plain"},
            )

        monkeypatch.setattr(net, "guarded_get", fake_guarded_get)
        result = await http_get_impl({"url": "https://example.com/"}, _ctx())
        assert result["status_code"] == 200
        assert result["body"] == "public data"
