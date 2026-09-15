"""Environment Planner — resolves requirements into concrete plans (ADR-0038).

The planner takes a validated `EnvironmentRequirement` and resolves it
into a concrete `EnvironmentPlan`: which workspace will be provisioned,
what limits apply, what network policy is enforced, what credential
handles are available.

The planner NEVER exposes host paths or raw secrets to the intelligence.
It returns opaque IDs (workspace_id, credential_handles) that the
runtime resolves inside the approved boundary.

Today the planner is a thin layer over the existing ProvisioningService
(for workspaces) and the existing isolation boundary (for namespace
sandboxes). Phase 7 will add the credential vault; until then,
credential requirements are recorded in the plan but the handles
list is empty (the vault boundary is not yet wired).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.runtime.environment.contracts import (
    ENVIRONMENT_PURPOSE_MAX_CHARS,
    EnvironmentLease,
    EnvironmentPlan,
    EnvironmentState,
    EnvironmentValidationError,
    IsolationGrade,
    NetworkPolicy,
    NetworkPolicyKind,
    validate_environment_requirement,
)
from wax.runtime.logging import get_logger
from wax.runtime.provisioning import MAX_TTL, ProvisioningService
from wax.state.environment_models import EnvironmentLeaseRecord

log = get_logger(__name__)


# Default lease TTL — environments are short-lived unless promoted.
DEFAULT_LEASE_TTL = timedelta(hours=1)
MAX_LEASE_TTL = MAX_TTL  # 24 hours — same as provisioning


class EnvironmentPlanner:
    """Resolves an EnvironmentRequirement into a concrete EnvironmentPlan.

    The planner is the SOLE place where the runtime decides what
    resources an environment gets. The intelligence declares; the
    planner resolves; the runtime provisions.

    The planner is stateless — it operates on the caller's session.
    """

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._provisioning = ProvisioningService(settings)

    async def plan(
        self,
        session: AsyncSession,
        *,
        principal_id: str,
        execution_id: str | None,
        requirement_dict: dict[str, Any],
    ) -> EnvironmentPlan:
        """Validate + plan. Does NOT provision — provisioning happens on
        lease creation (so a planned-but-not-leased environment owns no
        resources)."""
        requirement = validate_environment_requirement(requirement_dict)

        degraded: list[str] = []
        workspace_id: str | None = None
        credential_handles: list[str] = []
        notes_parts: list[str] = []

        # Credential requirements — recorded but not yet resolved
        # (Phase 7 will wire the credential vault). For now, the plan
        # notes that credentials were requested but the vault is not
        # available; the intelligence can decide whether to proceed.
        if requirement.credentials:
            notes_parts.append(
                f"credentials requested ({len(requirement.credentials)}); "
                "vault boundary not yet wired (Phase 7)"
            )

        # Network policy — record what was requested. The runtime
        # enforces whatever the policy declares.
        network_policy = requirement.network or NetworkPolicy()
        if network_policy.kind == NetworkPolicyKind.OPEN:
            # OPEN requires human approval (Phase 7 approval gate).
            # For now, degrade to ALLOWLISTED with an empty list
            # (effectively NONE) and record the degradation.
            degraded.append("network.kind=open requires human approval; degraded to none")
            network_policy = NetworkPolicy(kind=NetworkPolicyKind.NONE)

        # Isolation grade — record what was requested. CONTAINER is
        # not yet implemented; degrade to NAMESPACE with a note.
        isolation_grade = requirement.isolation
        if isolation_grade == IsolationGrade.CONTAINER:
            degraded.append("isolation=container not yet implemented; degraded to namespace")
            isolation_grade = IsolationGrade.NAMESPACE

        # Resource limits — record what was declared. Enforcement is
        # by the isolation boundary (when the environment is provisioned).
        resource_limits: dict[str, Any] = {}
        if requirement.execution:
            resource_limits["execution"] = {
                "cpu_seconds": requirement.execution.cpu_seconds,
                "memory_bytes": requirement.execution.memory_bytes,
                "processes": requirement.execution.processes,
                "timeout_seconds": requirement.execution.timeout_seconds,
            }
        if requirement.workspace:
            resource_limits["workspace"] = {
                "persistent": requirement.workspace.persistent,
                "disk_bytes": requirement.workspace.disk_bytes,
            }

        notes = "; ".join(notes_parts) if notes_parts else None

        return EnvironmentPlan(
            requirement=requirement,
            workspace_id=workspace_id,
            resource_limits=resource_limits,
            network_policy=network_policy,
            isolation_grade=isolation_grade,
            credential_handles=credential_handles,
            degraded=degraded,
            notes=notes,
        )

    async def provision_lease(
        self,
        session: AsyncSession,
        *,
        principal_id: str,
        execution_id: str | None,
        plan: EnvironmentPlan,
        ttl_seconds: int | None = None,
    ) -> EnvironmentLease:
        """Provision the environment per the plan and return a lease.

        This is the SOLE place an environment lease is created. The
        lease record is persisted BEFORE any resources are allocated —
        so a crash leaves honest state.
        """
        ttl = timedelta(seconds=ttl_seconds) if ttl_seconds else DEFAULT_LEASE_TTL
        if ttl <= timedelta(0):
            raise EnvironmentValidationError("ttl_seconds must be positive")
        if ttl > MAX_LEASE_TTL:
            raise EnvironmentValidationError(f"ttl_seconds exceeds the {MAX_LEASE_TTL} maximum")

        expires_at = datetime.now(UTC) + ttl
        environment_id = str(ULID())

        # Persist the lease record FIRST (crash-safe).
        record = EnvironmentLeaseRecord(
            id=environment_id,
            principal_id=principal_id,
            execution_id=execution_id,
            purpose=plan.requirement.purpose[:ENVIRONMENT_PURPOSE_MAX_CHARS],
            status=EnvironmentState.PROVISIONED.value,
            plan_json={
                "requirement_purpose": plan.requirement.purpose,
                "workspace_id": plan.workspace_id,
                "resource_limits": plan.resource_limits,
                "network_policy": {
                    "kind": plan.network_policy.kind.value,
                    "allowlist": plan.network_policy.allowlist,
                },
                "isolation_grade": plan.isolation_grade.value,
                "credential_handles": plan.credential_handles,
                "degraded": plan.degraded,
                "notes": plan.notes,
            },
            expires_at=expires_at,
            workspace_resource_id=None,  # set below if workspace provisioned
            error=None,
        )
        session.add(record)
        await session.flush()

        # Provision workspace if required
        workspace_resource_id: str | None = None
        if plan.requirement.workspace:
            try:
                workspace = await self._provisioning.provision_scratch_dir(
                    session,
                    principal_id=principal_id,
                    ttl_seconds=int(ttl.total_seconds()),
                    execution_id=execution_id,
                    limits=plan.resource_limits.get("workspace", {}),
                )
                workspace_resource_id = workspace.id
                record.workspace_resource_id = workspace_resource_id
                await session.flush()
            except Exception as e:
                # Workspace provisioning failed — mark the lease as failed
                # honestly. The intelligence can retry or degrade.
                record.status = EnvironmentState.FAILED.value
                record.error = f"workspace provisioning failed: {e}"
                await session.flush()
                log.warning(
                    "environment.provision_failed",
                    environment_id=environment_id,
                    error=str(e)[:500],
                )
                raise

        log.info(
            "environment.provisioned",
            environment_id=environment_id,
            principal_id=principal_id,
            workspace_resource_id=workspace_resource_id,
            isolation=plan.isolation_grade.value,
        )

        return EnvironmentLease(
            environment_id=environment_id,
            plan=plan,
            expires_at=expires_at,
            owner_execution_id=execution_id or "",
            state=EnvironmentState.PROVISIONED,
        )

    async def release(
        self,
        session: AsyncSession,
        environment_id: str,
        *,
        owner_execution_id: str | None = None,
    ) -> bool:
        """Release an environment lease. Fenced: only the owner can release."""
        record = await session.get(EnvironmentLeaseRecord, environment_id)
        if record is None:
            return False
        if record.status in (
            EnvironmentState.RELEASED.value,
            EnvironmentState.EXPIRED.value,
            EnvironmentState.FAILED.value,
        ):
            return False
        if (
            owner_execution_id is not None
            and record.execution_id is not None
            and record.execution_id != owner_execution_id
        ):
            log.warning(
                "environment.release_fenced",
                environment_id=environment_id,
                owner=record.execution_id,
                attempted_by=owner_execution_id,
            )
            return False

        record.status = EnvironmentState.RELEASED.value
        await session.flush()

        # Release the workspace resource if bound
        if record.workspace_resource_id:
            try:
                await self._provisioning.release(session, record.workspace_resource_id)
            except Exception as e:
                log.warning(
                    "environment.workspace_release_failed",
                    environment_id=environment_id,
                    workspace_id=record.workspace_resource_id,
                    error=str(e)[:300],
                )

        log.info(
            "environment.released",
            environment_id=environment_id,
        )
        return True

    async def get_lease(
        self, session: AsyncSession, environment_id: str
    ) -> EnvironmentLeaseRecord | None:
        return await session.get(EnvironmentLeaseRecord, environment_id)
