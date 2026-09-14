"""Pydantic contracts for the capability system.

A capability is defined by:
- name: stable identifier (e.g. "http.get", "web.search", "memory.read")
- description: what it does (for the model and registry)
- input_schema: JSON schema for valid inputs
- output_schema: JSON schema for outputs
- required_permission: the permission required to invoke this capability
- timeout_seconds: max execution time
- idempotent: whether duplicate invocations are safe

A capability implementation is a callable that takes inputs and returns
outputs. The contract is stable; the implementation is replaceable (INV-07).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


@dataclass
class InvocationContext:
    """Who is invoking, and under what runtime trace.

    Passed to every capability implementation as the second argument.
    Capabilities must not trust it for authorization — the invoker has
    already enforced permissions before the implementation runs.
    """

    principal_id: str
    capability_name: str
    execution_id: str
    request_id: str | None = None


class CapabilityStatus(StrEnum):
    """Lifecycle status of a capability in the registry."""

    REGISTERED = "registered"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    REVOKED = "revoked"


class CapabilityDescriptor(BaseModel):
    """Describes a capability. Stable contract; implementation is replaceable."""

    name: str = Field(..., description="Stable identifier, e.g. 'http.get'")
    description: str = Field(..., description="Human-readable description")
    version: str = Field(default="1.0.0")
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        description="JSON schema for inputs",
    )
    output_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object"},
        description="JSON schema for outputs",
    )
    required_permission: str = Field(
        ..., description="Permission required to invoke (e.g. 'capability.invoke:http.get')"
    )
    timeout_seconds: float = Field(default=30.0, ge=0.1, le=600.0)
    idempotent: bool = Field(
        default=False,
        description="Whether duplicate invocations are safe",
    )
    is_destructive: bool = Field(
        default=False,
        description="Whether this capability causes irreversible effects",
    )


class CapabilityInvocationRequest(BaseModel):
    """A request from the intelligence to invoke a capability.

    The runtime validates:
    - the capability exists
    - the requesting principal has the required permission
    - the inputs match the input_schema
    - the idempotency key (if provided) is claimed exactly once
      (CV-19): a replay returns the RECORDED outcome of the first
      execution; a concurrent duplicate is refused until the first
      attempt completes or its claim lease expires
    """

    capability_name: str
    principal_id: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(
        default=None,
        description="Caller-chosen at-most-once handle for this request shape",
    )
    request_id: str | None = None


class CapabilityInvocationResult(BaseModel):
    """The outcome of a capability invocation.

    The runtime returns this to the intelligence. The intelligence NEVER
    sees raw infrastructure errors — they are wrapped in a structured
    result with `outcome` discriminator.
    """

    capability_name: str
    outcome: str  # success | denied | failure | timeout | not_found | duplicate
    outputs: dict[str, Any] | None = None
    error: str | None = None
    execution_id: str
    started_at: datetime
    ended_at: datetime
    duration_ms: float
    # CV-19: True when this result is the RECORDED outcome of an earlier
    # identical invocation (same idempotency key) — the effect did not
    # run again.
    idempotent_replay: bool = False
