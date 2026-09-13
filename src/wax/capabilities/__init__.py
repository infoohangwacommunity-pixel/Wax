"""wax.capabilities — capability contracts, registry, and invocation.

A capability is an authorized mechanism through which intelligence can cause
an external or internal effect (Directive §34).

Architecture:
- Capability CONTRACTS live in wax.capabilities.contracts (Pydantic models)
- Capability IMPLEMENTATIONS live in wax.capabilities.built_ins (actual tools)
- Capability REGISTRY tracks what exists, what's available, what's authorized
- Capability INVOKER is the single point where the AI requests an action and
  the runtime decides whether to perform it (INV-04)

INVARIANT INV-04: AI-requested actions must pass through runtime authorization.
Every capability invocation MUST go through AuthorizationService.check()
before the capability is executed.
"""

from wax.capabilities.contracts import (
    CapabilityDescriptor,
    CapabilityInvocationRequest,
    CapabilityInvocationResult,
    CapabilityStatus,
)
from wax.capabilities.registry import CapabilityRegistry
from wax.capabilities.invoker import CapabilityInvoker

__all__ = [
    "CapabilityDescriptor",
    "CapabilityInvocationRequest",
    "CapabilityInvocationResult",
    "CapabilityStatus",
    "CapabilityRegistry",
    "CapabilityInvoker",
]
