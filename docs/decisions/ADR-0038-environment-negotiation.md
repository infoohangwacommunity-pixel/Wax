# ADR-0038: Environment Negotiation

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 5 — Environment Negotiation (OMEGA Implementation Directive)

## Context

WAX already has dynamic provisioning (`ProvisioningService` for scratch
directories) and code-execution isolation (`wax.isolation`). But the
intelligence has no way to DECLARE what kind of environment it needs.
Today, `code.run` is a capability that takes a Python snippet and runs
it in a namespace sandbox; the sandbox shape is fixed.

The directive (Phase 5) requires a generic negotiation protocol:

> The intelligence declares requirements. The runtime plans resources.

This is the universal primitive that unlocks Phases 6-9:

- Phase 6 (Terminal Runtime): a terminal session IS an environment with
  a workspace, process group, network policy, credentials.
- Phase 7 (Credential Vault): credentials are injected into an
  environment boundary, not exposed to the model.
- Phase 8 (Connector Runtime): a connector discovery runs inside an
  environment that has network access + credentials.
- Phase 9 (Workspace + Artifact Lifecycle): workspaces ARE
  environments; artifacts are captured from them.

## Decision

Add six universal contracts in `wax.runtime.environment.contracts`:

### 1. `EnvironmentRequirement`

The intelligence's declaration of what it needs. Bounded schema:

```python
@dataclass(frozen=True)
class EnvironmentRequirement:
    purpose: str  # human-readable, ≤ 200 chars; never secrets
    workspace: WorkspaceRequirement | None  # persistent disk
    execution: ExecutionRequirement | None  # CPU/memory/process/time
    tools: list[ToolRequirement]  # what tools must be available
    credentials: list[CredentialRequirement]  # what scopes are needed
    network: NetworkPolicy | None  # egress policy
    isolation: str  # "none" | "namespace" | "container"
```

### 2. `EnvironmentPlan`

The runtime's resolution of a requirement into a concrete plan:

```python
@dataclass(frozen=True)
class EnvironmentPlan:
    requirement: EnvironmentRequirement
    workspace_id: str | None  # opaque ID
    resource_limits: dict[str, Any]  # enforced limits
    network_policy: NetworkPolicy
    isolation_grade: str
    credential_handles: list[str]  # opaque, never raw secrets
    degraded: list[str]  # requirements that could not be fully met
    notes: str | None
```

### 3. `EnvironmentLease`

A bounded lease on a provisioned environment:

```python
@dataclass(frozen=True)
class EnvironmentLease:
    environment_id: str  # opaque ID
    plan: EnvironmentPlan
    expires_at: datetime
    owner_execution_id: str  # fencing
```

### 4. `EnvironmentState`

The live state of the environment:

```python
class EnvironmentState(StrEnum):
    PLANNED = "planned"
    PROVISIONED = "provisioned"
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"
    FAILED = "failed"
```

### 5. `EnvironmentEvent`

Lifecycle events emitted to the runtime signal ledger:

- `environment.planned:<id>`
- `environment.provisioned:<id>`
- `environment.released:<id>`
- `environment.expired:<id>`

### 6. `EnvironmentCapabilityBinding`

A binding that says "capability X is bound to environment Y" — so a
capability invocation can verify it has the right environment before
running. Stored in the new `environment_capability_bindings` table.

### Capability: `environment.request`

The intelligence calls this with an `EnvironmentRequirement`. The
runtime:

1. Validates the requirement shape (size limits, no secrets).
2. Checks authority (the principal must have `environment.request`
   permission).
3. Plans the environment (resolves workspace, isolation, network,
   credentials).
4. Provisions the environment (creates the workspace, sets up the
   isolation boundary, injects credentials via the vault boundary).
5. Returns an `EnvironmentLease` with opaque handles.

The intelligence then uses the lease's `environment_id` when invoking
capabilities that require an environment (e.g. `terminal.execute`).

### Persistence

A new table `environment_leases` records each provisioned environment
with its plan, owner, TTL, and state. The existing `provisioned_resources`
table is reused for workspaces (kind = "environment_workspace").

### Architecture boundary

The environment contracts live in `wax.runtime.environment` — a new
package. The intelligence sees only the contracts + the capability; it
never imports the planner or the isolation boundary. The composition
root wires the planner to the runtime services.

## Alternatives considered

### Alternative 1: Bake environment selection into each capability

Rejected — `terminal.execute`, `code.run`, `http.get`, etc. would each
need to know about workspaces, isolation, and credentials. That's
exactly the "duplicate logic" the constitution forbids (Law 6).

### Alternative 2: A single "execution_environment" parameter on every capability

Considered — but it would couple every capability to the environment
abstraction. Some capabilities (echo, memory.store) need no environment.
The binding table makes the relationship optional and explicit.

## Consequences

### Positive

- The intelligence can declare requirements without choosing unsafe
  host-level details (no host paths, no raw secrets, no privileged
  containers).
- New environment kinds (containers, browser sessions) absorb by
  extending the planner, not by rewriting capabilities.
- The binding table lets capabilities verify "I have the right
  environment" before running — a universal safety check.

### Negative

- One new migration (environment_leases + environment_capability_bindings
  tables).
- The planner is a new component that must be tested at every boundary
  (workspace allocation failure, isolation setup failure, credential
  injection failure).

## Security

- The `EnvironmentRequirement.purpose` field is bounded (≤ 200 chars)
  and validated to contain no secret-like patterns.
- Credential handles are opaque IDs, never raw secrets. The vault
  (Phase 7, future) injects secrets only inside the approved
  environment boundary.
- The planner NEVER exposes host paths to the intelligence. The
  workspace ID is opaque; the host filesystem layout is an
  implementation detail.

## Tests

`tests/integration/test_environment_negotiation.py` covers:

- environment.request with a workspace requirement
- environment.request with isolation requirement
- environment.request with credential requirement (vault boundary —
  mocked for now; full vault is Phase 7)
- environment.request with degraded requirements (some unmet)
- environment lease expires and is reclaimed
- capability binding rejects invocation without the right environment
- payload validation (size limits, no secrets)
