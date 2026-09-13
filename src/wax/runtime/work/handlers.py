"""Work handlers — what the runner can DO with durable work.

Exactly one handler exists: "capability". It is the universal mechanism:
wake at a time and invoke a registered capability on behalf of the
principal who scheduled the work, through the same gate chain the live
bridge uses (agency → budget → authority → invoker).

Because the handler is generic, a reminder is not a feature — it is the
composition work.schedule(kind="capability", payload={capability_name:
"message.send", ...}, wake_at=...) — and any FUTURE capability is
automatically schedulable without new runtime code.
"""

from __future__ import annotations

from typing import Any

from wax.objective.evidence import (
    sync_active_for_execution,
    sync_awaiting_human_for_execution,
)
from wax.runtime.logging import get_logger
from wax.runtime.services import RuntimeServices
from wax.runtime.work.runner import WorkExecutionError
from wax.state.engine import db_session
from wax.state.work_models import WorkItemRecord

log = get_logger(__name__)


async def record_work_participation(item: WorkItemRecord) -> None:
    """Append the objective-execution history row for this work run (ADR-0020).

    Tolerant: an unresolvable objective (no history, no pointer) simply
    records nothing — the work still runs. Must never break execution.
    """
    from wax.objective.evidence import objective_for_execution
    from wax.objective.repository import ObjectiveRepository

    if not item.execution_id:
        return
    try:
        async with db_session() as session:
            objective = await objective_for_execution(session, item.execution_id)
            if objective is None:
                return
            await ObjectiveRepository(session).record_execution_start(
                objective.id, item.id, kind="work"
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001 — history must never break work
        log.warning(
            "objective.work_history_failed", work_id=item.id, reason=str(e)[:300]
        )


async def capability_handler(services: RuntimeServices, item: WorkItemRecord) -> dict[str, Any]:
    """Wake and invoke a capability on behalf of the item's principal."""
    from wax.agency.contracts import AgencyDecision, AgencyDecisionKind
    from wax.capabilities.contracts import (
        CapabilityInvocationRequest,
        CapabilityStatus,
    )
    from wax.resources.contracts import ResourceKind, ResourceUsage

    payload = dict(item.payload or {})
    capability_name = payload.get("capability_name")
    if not capability_name:
        raise WorkExecutionError("payload.capability_name is required")
    inputs = payload.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise WorkExecutionError("payload.inputs must be an object")

    # The work must have an owner: capability work without a principal has
    # no authority to act under.
    if not item.principal_id:
        raise WorkExecutionError("work item has no principal_id; cannot authorize")

    # Budget for the invocation — durable work spends from its own bucket.
    budget_key = f"work-{item.id}"
    services.resource_accountant.allocate(
        budget_key, capability_invocations=1.0, execution_time_seconds=300.0
    )

    async with db_session() as session:
        try:
            descriptor, _impl = services.capability_registry.get(capability_name)
        except Exception as e:
            raise WorkExecutionError(f"No such capability: {capability_name}") from e
        if services.capability_registry.get_status(capability_name) is not (
            CapabilityStatus.AVAILABLE
        ):
            raise WorkExecutionError(f"Capability {capability_name} is not currently available")

        # Agency gate (same policy as the live path). Actions that require
        # explicit human authorization enter the approval primitive: the
        # pending approval is created/returned idempotently, and the attempt
        # fails honestly — a later attempt (or a work.requeue after the
        # human approves) consumes the approval exactly once.
        agency = services.agency(session)
        decision = AgencyDecision(
            principal_id=item.principal_id,
            kind=(
                AgencyDecisionKind.DESTRUCTIVE_ACTION
                if descriptor.is_destructive
                else AgencyDecisionKind.INVOKE_CAPABILITY
            ),
            description=f"Durable work {item.id} invokes {capability_name}",
            capability_name=capability_name,
            inputs_summary={k: str(v)[:120] for k, v in list(inputs.items())[:5]},
        )
        verdict = await agency.evaluate(decision)
        if not verdict.approved or verdict.requires_human_approval:
            from wax.authority.approvals import (
                ApprovalService,
                fingerprint_request,
            )
            from wax.runtime.work.signals import SignalRepository

            approvals = ApprovalService(session)
            fp = fingerprint_request(item.principal_id, capability_name, inputs)

            # An approved, unconsumed approval for this EXACT request
            # authorizes one attempt — consumed here, so the action runs
            # exactly once per human decision (replay impossible).
            approved = await approvals.find_approved_unconsumed(
                item.principal_id, fp
            )
            if approved is not None and await approvals.consume(
                approved.id, execution_id=item.execution_id
            ):
                await session.commit()
                log.info(
                    "approval.authorized_attempt",
                    approval_id=approved.id,
                    capability=capability_name,
                    work_id=item.id,
                )
                # Evidence sync (ADR-0020): the human approved — the
                # objective's wait on this decision is over.
                await sync_active_for_execution(session, item.execution_id)
                await session.commit()
            else:
                # No approval yet: create/return the pending request
                # idempotently, announce it, and fail this attempt honestly.
                record, created = await approvals.create_or_get_pending(
                    principal_id=item.principal_id,
                    capability_name=capability_name,
                    action_kind=verdict.level.value,
                    inputs=inputs,
                    requested_by_execution_id=item.execution_id,
                    expires_in_seconds=float(
                        services.settings.approval_expiry_seconds
                    ),
                )
                if created:
                    await SignalRepository(session).emit(
                        f"approval.requested:{item.principal_id}",
                        payload={"approval_id": record.id, "capability": capability_name},
                        emitted_by="work_runner",
                    )
                    # Evidence sync (ADR-0020): a pending approval created
                    # from durable work IS the objective awaiting a human.
                    await sync_awaiting_human_for_execution(
                        session, item.execution_id
                    )
                await session.commit()
                services.metrics.approval_requested()
                raise WorkExecutionError(
                    f"This scheduled action requires explicit human authorization. "
                    f"Approval {record.id} for capability {capability_name} is "
                    f"{record.status} until {record.expires_at.isoformat()}. The "
                    f"human can decide; the work can then be requeued."
                )

        if not services.resource_accountant.try_consume(
            ResourceUsage(budget_key, ResourceKind.CAPABILITY_INVOCATIONS, 1.0, notes="work")
        ):
            raise WorkExecutionError("Resource budget exhausted for work invocation")

        # Evidence sync (ADR-0020): the wait resolved — the objective is
        # active again, and this run joins its execution history.
        await sync_active_for_execution(session, item.execution_id)
        await session.commit()
        await record_work_participation(item)

        invoker = services.invoker(session)
        result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name=capability_name,
                principal_id=item.principal_id,
                inputs=inputs,
                request_id=item.id,
            )
        )
        await session.commit()

    services.metrics.capability_invoked(result.outcome, capability_name)
    if result.outcome != "success":
        # Non-success = failed attempt → retry/backoff semantics apply.
        raise WorkExecutionError(
            f"capability {capability_name} outcome={result.outcome}: {result.error}"
        )
    return result.outputs or {}
