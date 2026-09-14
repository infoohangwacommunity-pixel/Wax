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

from wax.objective.evidence import sync_active_for_execution
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
    from wax.capabilities.contracts import (
        CapabilityInvocationRequest,
        CapabilityStatus,
    )
    from wax.resources.contracts import ResourceKind, ResourceUsage

    payload = dict(item.payload or {})
    capability_name = payload.get("capability_name")
    if not capability_name:
        raise WorkExecutionError("payload.capability_name is required")
    raw_inputs = payload.get("inputs") or {}
    if not isinstance(raw_inputs, dict):
        raise WorkExecutionError("payload.inputs must be an object")

    # CV-19: lift the declared idempotency-key transport field (request
    # metadata, not operation semantics) before the authority gate.
    from wax.capabilities.invoker import lift_idempotency_key

    inputs, idempotency_key = lift_idempotency_key(raw_inputs)

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

        # Agency gate → approval chain (CV-12/CV-14 fix: the ONE shared
        # component — the same authority chain the live bridge runs, in
        # wax.authority.gate — not a private copy. A pending approval
        # created here notifies the human exactly like the bridge does.)
        from wax.authority.gate import ApprovalGate, ApprovalGateState

        gate = ApprovalGate(session, services)
        outcome = await gate.evaluate(
            principal_id=item.principal_id,
            capability_name=capability_name,
            descriptor=descriptor,
            inputs=inputs,
            execution_id=item.execution_id,
            description=f"Durable work {item.id} invokes {capability_name}",
            emitted_by="work_runner",
        )
        if outcome.state is ApprovalGateState.PENDING:
            record = outcome.approval
            await session.commit()
            raise WorkExecutionError(
                f"This scheduled action requires explicit human authorization. "
                f"Approval {record.id} for capability {capability_name} is "
                f"{record.status} until {record.expires_at.isoformat()}. The "
                f"human can decide; the work can then be requeued."
            )
        if outcome.state is ApprovalGateState.ALREADY_USED:
            await session.commit()
            raise WorkExecutionError(outcome.error or "Approval was already used.")
        # AUTHORIZED: the approval was consumed above (or not required) —
        # fall through to budget + invoker.

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
                idempotency_key=idempotency_key,
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
