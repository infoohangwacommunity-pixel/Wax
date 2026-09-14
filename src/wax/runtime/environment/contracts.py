"""Environment Negotiation — universal contracts (ADR-0038, Phase 5).

The intelligence DECLARES what it needs; the runtime PLANS resources.
The intelligence never chooses unsafe host-level details (no host
paths, no raw secrets, no privileged containers). The runtime provisions
bounded environments and returns opaque handles.

Six universal contracts:

- EnvironmentRequirement: the intelligence's declaration
- EnvironmentPlan: the runtime's resolution
- EnvironmentLease: a bounded lease on a provisioned environment
- EnvironmentState: the live state (planned/provisioned/active/...)
- EnvironmentEvent: lifecycle events emitted to the signal ledger
- EnvironmentCapabilityBinding: which capabilities are bound to which env

The intelligence sees only these contracts + the `environment.request`
capability. It NEVER imports the planner, the isolation boundary, or
the credential vault. The composition root wires those at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# Bounded sizes — the intelligence's requirement must fit in a
# reasonable envelope (it becomes part of the persisted environment
# record).
ENVIRONMENT_PURPOSE_MAX_CHARS = 200
ENVIRONMENT_REQUIREMENT_MAX_BYTES = 16_000
ENVIRONMENT_PLAN_MAX_BYTES = 32_000


class EnvironmentState(StrEnum):
    """The live state of a provisioned environment."""

    PLANNED = "planned"
    PROVISIONED = "provisioned"
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    FAILED = "failed"


class IsolationGrade(StrEnum):
    """The isolation strength the runtime will enforce."""

    NONE = "none"
    NAMESPACE = "namespace"
    CONTAINER = "container"


class NetworkPolicyKind(StrEnum):
    """Egress policy for an environment."""

    NONE = "none"
    ALLOWLISTED = "allowlisted"
    OPEN = "open"


@dataclass(frozen=True)
class WorkspaceRequirement:
    persistent: bool = False
    disk_bytes: int = 0
    description: str | None = None


@dataclass(frozen=True)
class ExecutionRequirement:
    cpu_seconds: int = 0
    memory_bytes: int = 0
    processes: int = 0
    timeout_seconds: int = 0


@dataclass(frozen=True)
class ToolRequirement:
    name: str
    acquire_if_missing: bool = False


@dataclass(frozen=True)
class CredentialRequirement:
    connector: str
    scopes: list[str] = field(default_factory=list)
    purpose: str | None = None


@dataclass(frozen=True)
class NetworkPolicy:
    kind: NetworkPolicyKind = NetworkPolicyKind.NONE
    allowlist: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EnvironmentRequirement:
    purpose: str
    workspace: WorkspaceRequirement | None = None
    execution: ExecutionRequirement | None = None
    tools: list[ToolRequirement] = field(default_factory=list)
    credentials: list[CredentialRequirement] = field(default_factory=list)
    network: NetworkPolicy | None = None
    isolation: IsolationGrade = IsolationGrade.NONE


@dataclass(frozen=True)
class EnvironmentPlan:
    requirement: EnvironmentRequirement
    workspace_id: str | None
    resource_limits: dict[str, Any]
    network_policy: NetworkPolicy
    isolation_grade: IsolationGrade
    credential_handles: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    notes: str | None = None


@dataclass(frozen=True)
class EnvironmentLease:
    environment_id: str
    plan: EnvironmentPlan
    expires_at: datetime
    owner_execution_id: str
    state: EnvironmentState = EnvironmentState.PROVISIONED


class EnvironmentValidationError(ValueError):
    """Raised when an environment requirement fails validation."""


def validate_environment_requirement(req: dict[str, Any] | None) -> EnvironmentRequirement:
    """Validate a raw dict requirement and return the typed contract."""
    if req is None:
        raise EnvironmentValidationError("environment requirement is required")
    if not isinstance(req, dict):
        raise EnvironmentValidationError("environment requirement must be an object")

    purpose = req.get("purpose")
    if not isinstance(purpose, str):
        raise EnvironmentValidationError("purpose must be a string")
    if not purpose.strip():
        raise EnvironmentValidationError("purpose must not be empty")
    if len(purpose) > ENVIRONMENT_PURPOSE_MAX_CHARS:
        raise EnvironmentValidationError(
            f"purpose exceeds {ENVIRONMENT_PURPOSE_MAX_CHARS} chars"
        )

    workspace = None
    ws_raw = req.get("workspace")
    if ws_raw is not None:
        if not isinstance(ws_raw, dict):
            raise EnvironmentValidationError("workspace must be an object")
        workspace = WorkspaceRequirement(
            persistent=bool(ws_raw.get("persistent", False)),
            disk_bytes=int(ws_raw.get("disk_bytes", 0)),
            description=ws_raw.get("description"),
        )

    execution = None
    exec_raw = req.get("execution")
    if exec_raw is not None:
        if not isinstance(exec_raw, dict):
            raise EnvironmentValidationError("execution must be an object")
        execution = ExecutionRequirement(
            cpu_seconds=int(exec_raw.get("cpu_seconds", 0)),
            memory_bytes=int(exec_raw.get("memory_bytes", 0)),
            processes=int(exec_raw.get("processes", 0)),
            timeout_seconds=int(exec_raw.get("timeout_seconds", 0)),
        )

    tools_raw = req.get("tools") or []
    if not isinstance(tools_raw, list):
        raise EnvironmentValidationError("tools must be a list")
    if len(tools_raw) > 20:
        raise EnvironmentValidationError("tools list exceeds 20 items")
    tools: list[ToolRequirement] = []
    for t in tools_raw:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str):
            raise EnvironmentValidationError("each tool must be {name: str, ...}")
        tools.append(
            ToolRequirement(
                name=t["name"],
                acquire_if_missing=bool(t.get("acquire_if_missing", False)),
            )
        )

    creds_raw = req.get("credentials") or []
    if not isinstance(creds_raw, list):
        raise EnvironmentValidationError("credentials must be a list")
    if len(creds_raw) > 10:
        raise EnvironmentValidationError("credentials list exceeds 10 items")
    credentials: list[CredentialRequirement] = []
    for c in creds_raw:
        if not isinstance(c, dict) or not isinstance(c.get("connector"), str):
            raise EnvironmentValidationError(
                "each credential must be {connector: str, ...}"
            )
        scopes = c.get("scopes") or []
        if not isinstance(scopes, list):
            raise EnvironmentValidationError("credential.scopes must be a list")
        credentials.append(
            CredentialRequirement(
                connector=c["connector"],
                scopes=[str(s) for s in scopes],
                purpose=c.get("purpose"),
            )
        )

    network = None
    net_raw = req.get("network")
    if net_raw is not None:
        if not isinstance(net_raw, dict):
            raise EnvironmentValidationError("network must be an object")
        kind_str = net_raw.get("kind", "none")
        try:
            kind = NetworkPolicyKind(kind_str)
        except ValueError as e:
            raise EnvironmentValidationError(
                f"network.kind must be one of {[k.value for k in NetworkPolicyKind]}"
            ) from e
        allowlist = net_raw.get("allowlist") or []
        if not isinstance(allowlist, list):
            raise EnvironmentValidationError("network.allowlist must be a list")
        network = NetworkPolicy(
            kind=kind,
            allowlist=[str(h) for h in allowlist[:50]],
        )

    iso_str = req.get("isolation", "none")
    try:
        isolation = IsolationGrade(iso_str)
    except ValueError as e:
        raise EnvironmentValidationError(
            f"isolation must be one of {[k.value for k in IsolationGrade]}"
        ) from e

    import json

    try:
        req_bytes = len(json.dumps(req, default=str).encode("utf-8"))
    except (TypeError, ValueError) as e:
        raise EnvironmentValidationError(
            f"requirement is not JSON-serializable: {e}"
        ) from e
    if req_bytes > ENVIRONMENT_REQUIREMENT_MAX_BYTES:
        raise EnvironmentValidationError(
            f"requirement exceeds {ENVIRONMENT_REQUIREMENT_MAX_BYTES} bytes "
            f"(was {req_bytes})"
        )

    return EnvironmentRequirement(
        purpose=purpose,
        workspace=workspace,
        execution=execution,
        tools=tools,
        credentials=credentials,
        network=network,
        isolation=isolation,
    )
