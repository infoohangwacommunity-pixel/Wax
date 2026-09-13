"""Capability registry — tracks what capabilities exist and their status.

The registry is the source of truth for "what can WAX do?" The intelligence
queries the registry to discover capabilities (Directive §37 — capability
discovery). The invoker queries the registry to find the implementation
for a name.

This is an in-memory registry populated at startup. A future Phase J
(capability discovery) will add dynamic registration, health checks,
and external service discovery.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from wax.capabilities.contracts import (
    CapabilityDescriptor,
    CapabilityStatus,
    InvocationContext,
)
from wax.core.exceptions import WaxNotFoundError, WaxStateConflictError
from wax.runtime.logging import get_logger

log = get_logger(__name__)

# Type alias: a capability implementation is an async callable that takes
# inputs (dict) and returns outputs (dict).
CapabilityImpl = Callable[[dict[str, Any], InvocationContext], Awaitable[dict[str, Any]]]


class CapabilityRegistry:
    """In-memory registry of capability descriptors + implementations.

    The registry is intentionally simple — a dict of name → (descriptor, impl).
    A future Phase J will add:
    - dynamic registration (capabilities added at runtime)
    - health checks (a capability may degrade)
    - semantic discovery (find capabilities by description, not just name)
    - external service integration (capabilities served by other processes)
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, tuple[CapabilityDescriptor, CapabilityImpl]] = {}
        self._statuses: dict[str, CapabilityStatus] = {}

    def register(
        self,
        descriptor: CapabilityDescriptor,
        impl: CapabilityImpl,
    ) -> None:
        """Register a capability. Raises if a capability with the same name exists."""
        if descriptor.name in self._capabilities:
            raise WaxStateConflictError(
                f"Capability already registered: {descriptor.name}"
            )
        self._capabilities[descriptor.name] = (descriptor, impl)
        self._statuses[descriptor.name] = CapabilityStatus.AVAILABLE
        log.info(
            "capability.registered",
            name=descriptor.name,
            version=descriptor.version,
            required_permission=descriptor.required_permission,
        )

    def unregister(self, name: str) -> None:
        """Remove a capability from the registry."""
        if name not in self._capabilities:
            raise WaxNotFoundError(f"Capability not registered: {name}")
        del self._capabilities[name]
        self._statuses.pop(name, None)
        log.info("capability.unregistered", name=name)

    def get(self, name: str) -> tuple[CapabilityDescriptor, CapabilityImpl]:
        """Return (descriptor, impl) for a capability, or raise NotFound."""
        if name not in self._capabilities:
            raise WaxNotFoundError(f"Capability not found: {name}")
        return self._capabilities[name]

    def list_capabilities(self) -> list[CapabilityDescriptor]:
        """Return descriptors for all registered capabilities."""
        return [desc for desc, _ in self._capabilities.values()]

    def get_status(self, name: str) -> CapabilityStatus:
        """Return the current status of a capability."""
        if name not in self._statuses:
            return CapabilityStatus.UNAVAILABLE
        return self._statuses[name]

    def set_status(self, name: str, status: CapabilityStatus) -> None:
        """Update the status of a capability (e.g. mark degraded)."""
        if name not in self._capabilities:
            raise WaxNotFoundError(f"Capability not registered: {name}")
        self._statuses[name] = status
        log.info("capability.status_changed", name=name, status=status.value)

    def __len__(self) -> int:
        return len(self._capabilities)

    def __contains__(self, name: object) -> bool:
        return name in self._capabilities
