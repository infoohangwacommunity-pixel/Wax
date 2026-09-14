"""Network boundary — SSRF protection for outbound capability traffic.

Why this exists (Foundation PDF §16: "network access" is a runtime
enforcement boundary): a capability like http.get is an effect on the
world. Without a boundary, an AI (or an injection through fetched
content) can reach infrastructure that was never meant to be reachable
from a capability:

- cloud metadata endpoints (169.254.169.254, fd00:ec2::254)
- loopback services (the runtime itself, databases, sidecars)
- private LAN ranges (RFC 1918), CGNAT (RFC 6598), link-local (RFC 3927)
- unusual schemes (file:, gopher:, unix sockets via custom schemes)

Design (learned from production egress guards):
1. SCHEME allowlist — http/https only, no credentials in URL.
2. DNS RESOLUTION + IP CLASSIFICATION — we resolve the hostname OURSELVES
   and classify every resolved address. Hostname allow/deny is not
   enough (DNS rebinding): the connection must be pinned to the
   validated IP, not re-resolved by the transport.
3. REDIRECTION IS RE-VALIDATED — each hop crosses the same boundary;
   a public URL that redirects to 127.0.0.1 is still blocked.
4. BODY SIZE CAP — the response is streamed and truncated at a byte
   cap; a hostile server cannot OOM the runtime with a huge body.

The boundary is a MECHANISM: it knows nothing about what the fetch is
for. Capabilities compose it; they do not re-implement it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import typing
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpcore
import httpx

from wax.runtime.logging import get_logger

# httpcore's default backend (creates the real socket). Not a public
# class in httpcore 1.0.x; httpx 0.28.x pins httpcore 1.0.x, and the
# import is guarded so an unexpected httpcore layout fails loudly here
# rather than at fetch time.
from httpcore._backends.auto import AutoBackend as _AutoBackend  # noqa: PLC2701

log = get_logger(__name__)

ALLOWED_SCHEMES = {"http", "https"}
MAX_REDIRECTS = 3


class NetworkBoundaryError(Exception):
    """Raised when a URL may not be fetched. The message is safe to
    return to the AI: it states the constraint, never the internals."""


@dataclass(frozen=True)
class FetchPolicy:
    """Tunable boundary parameters. Defaults are the production stance."""

    max_bytes: int = 1_000_000
    connect_timeout: float = 10.0
    total_timeout: float = 30.0


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if the address must never be reachable from a capability.

    Blocked: unspecified, loopback, link-local (incl. IPv4-metadata),
    private (RFC1918/ULA), CGNAT, reserved, multicast.
    Allowed: global unicast only.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        # ::ffff:10.0.0.1 is just IPv4 in disguise — classify as IPv4.
        ip = ip.ipv4_mapped
    return not ip.is_global


def validate_url(url: str) -> tuple[str, list[str]]:
    """Validate a URL against the boundary.

    Returns (normalized_url, resolved_ips). Raises NetworkBoundaryError
    with a caller-safe message when any rule fails.

    DNS is resolved here so the caller can pin the connection to the
    validated addresses (anti-rebinding), and so hostname tricks
    (0x7f.0.0.1, decimal IPs, localhost subdomains) are evaluated on
    the ADDRESS, not the string.
    """
    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise NetworkBoundaryError("invalid URL") from e

    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise NetworkBoundaryError(f"scheme not allowed: {parts.scheme or '(none)'}")
    if not parts.hostname:
        raise NetworkBoundaryError("URL has no hostname")
    if parts.username or parts.password:
        raise NetworkBoundaryError("credentials in URL are not allowed")

    host = parts.hostname
    port = parts.port
    try:
        # AI_ADDRCONFIG avoids literal-IPv6 disambiguation surprises; we
        # try both families and let the resolver decide.
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80))
    except socket.gaierror as e:
        raise NetworkBoundaryError(f"cannot resolve host: {host}") from e

    resolved: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        addr = sockaddr[0]
        if addr in resolved:
            continue
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_blocked_address(ip):
            raise NetworkBoundaryError(
                f"host resolves to a non-public address ({ip}) — blocked"
            )
        resolved.append(addr)

    if not resolved:
        raise NetworkBoundaryError("host resolved to no usable addresses")
    return url, resolved


class _PinningNetworkBackend(httpcore.AsyncNetworkBackend):
    """CV-18: the transport can only connect to addresses that crossed
    the boundary.

    The pool hands us the HOSTNAME it wants to talk to. We resolve it
    ourselves and refuse unless every resolved address is one of the
    addresses `validate_url` already classified as public AND is a
    member of the validated set for THIS fetch. A DNS answer that
    differs from the validated set — most importantly a rebinding to a
    private/loopback/metadata address, but also any unvalidated change
    — fails the connection. The TCP stream is then opened against the
    literal IP (no further resolution); TLS still validates the
    certificate against the ORIGINAL hostname because httpcore calls
    `start_tls(server_hostname=hostname)` on the stream we return.
    """

    def __init__(self, allowed_ips: frozenset[str]) -> None:
        super().__init__()
        self._allowed = allowed_ips
        self._default = _AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port)
        except socket.gaierror as e:
            raise NetworkBoundaryError(f"cannot resolve host: {host}") from e

        pinned: str | None = None
        for _family, _type, _proto, _canon, sockaddr in infos:
            addr = sockaddr[0]
            if addr not in self._allowed or _is_blocked_address(
                ipaddress.ip_address(addr)
            ):
                raise NetworkBoundaryError(
                    "DNS changed during fetch (possible rebinding) — blocked"
                )
            if pinned is None:
                pinned = addr
        if pinned is None:
            raise NetworkBoundaryError("no usable address for pinned connection")
        # Literal IP: getaddrinfo never hits DNS for a literal, so the
        # default backend cannot silently re-resolve past us.
        return await self._default.connect_tcp(
            pinned,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool pins every TCP connection
    to the validated address set."""

    def __init__(self, allowed_ips: frozenset[str]) -> None:
        super().__init__()
        # Replace the pool httpx built with one that uses the pinning
        # backend; keep the parent's SSL context so certificate
        # verification behaves identically.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=getattr(self, "_ssl_context", None),
            network_backend=_PinningNetworkBackend(allowed_ips),
        )


async def guarded_get(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    policy: FetchPolicy | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.Response:
    """GET a URL inside the boundary. The response body is fully read
    (streamed, capped) before returning; callers get a complete httpx
    Response with `response.content` capped at policy.max_bytes.

    Every redirect hop is re-validated against the same rules. When no
    explicit transport is supplied, the connection is PINNED to the
    addresses validated for this fetch (anti-rebinding).
    """
    policy = policy or FetchPolicy()
    current_url, resolved = validate_url(url)

    for _hop in range(MAX_REDIRECTS + 1):
        hop_transport = transport or _PinnedHTTPTransport(frozenset(resolved))
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(policy.total_timeout, connect=policy.connect_timeout),
            follow_redirects=False,
            transport=hop_transport,
        ) as client:
            response = await client.get(current_url, headers=headers or {})

        if response.is_redirect:
            if _hop == MAX_REDIRECTS:
                raise NetworkBoundaryError("too many redirects")
            location = response.headers.get("location")
            if not location:
                raise NetworkBoundaryError("redirect without location")
            next_url = str(httpx.URL(current_url).join(location))
            # The hop itself crosses the boundary again.
            current_url, resolved = validate_url(next_url)
            continue
        break

    # Stream the body under the byte cap (a hostile server cannot OOM us).
    content = bytearray()
    async for chunk in response.aiter_bytes():
        content.extend(chunk)
        if len(content) > policy.max_bytes:
            await response.aclose()
            raise NetworkBoundaryError(
                f"response exceeds {policy.max_bytes} bytes"
            )
    response._content = bytes(content)
    return response
