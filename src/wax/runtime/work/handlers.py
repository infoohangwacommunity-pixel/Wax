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

from wax.runtime.logging import get_logger
from wax.runtime.services import RuntimeServices
from wax.runtime.work.runner import WorkExecutionError
from wax.state.engine import db_session
from wax.state.work_models import WorkItemRecord

log = get_logger(__name__)


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

        # Agency gate (same policy as the live path).
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
            raise WorkExecutionError(
                f"Runtime gate: {verdict.reason}. This scheduled capability "
                "cannot run without human approval."
            )

        if not services.resource_accountant.try_consume(
            ResourceUsage(budget_key, ResourceKind.CAPABILITY_INVOCATIONS, 1.0, notes="work")
        ):
            raise WorkExecutionError("Resource budget exhausted for work invocation")

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
