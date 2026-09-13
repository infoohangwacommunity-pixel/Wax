"""Built-in capabilities — the initial capability catalogue.

Built-in capabilities are simple, deterministic, and useful across domains.
They are NOT domain-specific (no "tutor.explain", no "lesson.generate").

Built-ins:
- echo: returns its inputs (smoke-test capability)
- http.get: performs an HTTP GET request (httpx-backed)

Each built-in registers a CapabilityDescriptor and an async implementation
function in the provided CapabilityRegistry.
"""

from __future__ import annotations

from typing import Any

from wax.capabilities.contracts import (
    CapabilityDescriptor,
    InvocationContext,
)
from wax.capabilities.registry import CapabilityRegistry


async def echo_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    """Trivial capability — returns its inputs. For smoke tests only."""
    return {"echo": inputs}


async def http_get_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    """Perform an HTTP GET request inside the runtime's network boundary.

    The boundary (wax.security.network) blocks private/loopback/metadata
    addresses, non-http schemes, credential-bearing URLs, and re-validates
    every redirect hop. Response bodies are streamed under a byte cap.

    Inputs:
        url: required, the URL to fetch (public http/https only)
        timeout_seconds: optional, default 10.0
        headers: optional, dict of headers

    Failures raise ValueError with caller-safe messages: the AI learns
    the real constraint, never internal topology.
    """
    from wax.security.network import (
        FetchPolicy,
        NetworkBoundaryError,
        guarded_get,
    )

    url = inputs.get("url")
    if not url:
        raise ValueError("Missing required input: url")

    timeout = float(inputs.get("timeout_seconds", 10.0))
    headers = inputs.get("headers", {}) or {}
    policy = FetchPolicy(total_timeout=max(timeout, 5.0))

    try:
        response = await guarded_get(url, headers=headers, policy=policy)
    except NetworkBoundaryError as e:
        raise ValueError(f"blocked by network boundary: {e}") from e

    text = response.text
    return {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": text[:10000],  # truncate to keep tool results bounded
        "body_truncated": len(text) > 10000,
    }


ECHO_DESCRIPTOR = CapabilityDescriptor(
    name="echo",
    description="Returns its inputs. Smoke-test capability.",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"message": {"type": "string"}},
    },
    output_schema={
        "type": "object",
        "properties": {"echo": {"type": "object"}},
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)


HTTP_GET_DESCRIPTOR = CapabilityDescriptor(
    name="http.get",
    description="Perform an HTTP GET request.",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "timeout_seconds": {"type": "number", "default": 10.0},
            "headers": {"type": "object"},
        },
        "required": ["url"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status_code": {"type": "integer"},
            "headers": {"type": "object"},
            "body": {"type": "string"},
            "body_truncated": {"type": "boolean"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=15.0,
    idempotent=True,
    is_destructive=False,
)


def register_builtins(registry: CapabilityRegistry) -> None:
    """Register all built-in capabilities in the given registry."""
    registry.register(ECHO_DESCRIPTOR, echo_impl)
    registry.register(HTTP_GET_DESCRIPTOR, http_get_impl)
