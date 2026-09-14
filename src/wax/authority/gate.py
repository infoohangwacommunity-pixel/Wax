"""ApprovalGate — the ONE shared approval chain (CV-12/CV-14 fix).

Before this module the approval side of the agency gate existed as TWO
hand-maintained copies: one in the live bridge
(``RuntimeBridge._approval_gate``) and one in the durable-work handler
(``work/handlers.py``). The copies had already drifted: only the bridge
notified the human, so an approval created by background work could
exist durably while the human never learned about it — authority state
the owner could not see. Duplication inside an authority boundary is
not a style problem; it is a correctness problem.

This module is the single component both paths consume:

    agency verdict
      → policy-approved            → AUTHORIZED (no approval needed)
      → approved, unconsumed
        approval for THIS EXACT
        request                    → consume it (one-time) → AUTHORIZED
      → equivalent pending
        approval exists            → PENDING (idempotent, no re-notify)
      → otherwise                  → create pending → event ledger →
                                     NOTIFY the human (live channel, or
                                     durable retry when none is up) →
                                     objective awaiting-human sync →
                                     PENDING

The callers differ only in HOW failures surface (a structured tool
result on the live path, a retryable WorkExecutionError on the work
path) — never in what authorization means.

Nothing here knows about any domain. Notification delivery is
interface-agnostic (DeliveryRouter + the principal's verified
credentials). This is pure authority infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from wax.runtime.logging import get_logger

log = get_logger(__name__)


class ApprovalGateState(StrEnum):
    """Outcome of the gate for one attempted action."""

    AUTHORIZED = "authorized"      # approval consumed (or not required); proceed
    PENDING = "pending"            # approval requested/pending; nothing executed
    ALREADY_USED = "already_used"  # approval was consumed elsewhere; refuse


@dataclass
class ApprovalGateOutcome:
    """What the gate decided and the evidence for it."""

    state: ApprovalGateState
    approval: Any | None = None  # PendingApprovalRecord, when one is involved
    error: str | None = None     # caller-safe explanation for refusals


class ApprovalGate:
    """The single approval chain behind the agency gate.

    Constructed per invocation with the caller's session; both the live
    bridge and the durable-work handler pass their own ``RuntimeServices``
    so the gate reads the same settings, delivery router, and metrics.
    """

    def __init__(self, session: AsyncSession, services: Any) -> None:
        self._session = session
        self._services = services

    async def evaluate(
        self,
        *,
        principal_id: str,
        capability_name: str,
        descriptor: Any,
        inputs: dict[str, Any] | None,
        execution_id: str | None,
        description: str,
        emitted_by: str,
    ) -> ApprovalGateOutcome:
        """Run the full approval chain for one capability request.

        Returns AUTHORIZED when the action may proceed to budget +
        invoker; PENDING or ALREADY_USED otherwise. Never raises for
        expected authority states — those are outcomes, not errors.
        """
        from wax.agency.contracts import AgencyDecision, AgencyDecisionKind
        from wax.authority.approvals import ApprovalService, fingerprint_request
        from wax.objective.evidence import sync_active_for_execution
        from wax.runtime.work.signals import SignalRepository

        agency = self._services.agency(self._session)
        decision = AgencyDecision(
            principal_id=principal_id,
            kind=(
                AgencyDecisionKind.DESTRUCTIVE_ACTION
                if descriptor.is_destructive
                else AgencyDecisionKind.INVOKE_CAPABILITY
            ),
            description=description,
            capability_name=capability_name,
            inputs_summary={
                k: str(v)[:120] for k, v in list((inputs or {}).items())[:5]
            },
        )
        verdict = await agency.evaluate(decision)
        if verdict.approved and not verdict.requires_human_approval:
            return ApprovalGateOutcome(state=ApprovalGateState.AUTHORIZED)

        approvals = ApprovalService(self._session)
        fp = fingerprint_request(principal_id, capability_name, inputs)

        # 1. An approved, unconsumed approval for this EXACT request
        # authorizes one attempt — consumed here, so the action runs
        # exactly once per human decision (replay impossible).
        approved = await approvals.find_approved_unconsumed(
            principal_id,
            fp,
            approval_ttl_seconds=self._services.settings.approval_expiry_seconds,
        )
        if approved is not None:
            consumed = await approvals.consume(
                approved.id, execution_id=execution_id
            )
            if consumed:
                log.info(
                    "approval.authorized_attempt",
                    approval_id=approved.id,
                    capability=capability_name,
                    execution_id=execution_id,
                    emitted_by=emitted_by,
                )
                # Evidence sync (ADR-0020): the human decided — the
                # objective is active again. BOTH objectives reactivate:
                # the one that requested the approval (it was awaiting the
                # human) and the one consuming it.
                await sync_active_for_execution(self._session, execution_id)
                await sync_active_for_execution(
                    self._session, approved.requested_by_execution_id
                )
                return ApprovalGateOutcome(
                    state=ApprovalGateState.AUTHORIZED, approval=approved
                )
            return ApprovalGateOutcome(
                state=ApprovalGateState.ALREADY_USED,
                approval=approved,
                error="Approval was already used; request a new authorization.",
            )

        # 2. Pending equivalent? (idempotent)  3. Otherwise create + notify.
        record, created = await approvals.create_or_get_pending(
            principal_id=principal_id,
            capability_name=capability_name,
            action_kind=verdict.level.value,
            inputs=inputs,
            requested_by_execution_id=execution_id,
            expires_in_seconds=float(
                self._services.settings.approval_expiry_seconds
            ),
        )
        if created:
            await SignalRepository(self._session).emit(
                f"approval.requested:{principal_id}",
                payload={"approval_id": record.id, "capability": capability_name},
                emitted_by=emitted_by,
            )
            await self._notify(record)
            # Evidence sync (ADR-0020): a pending approval IS the
            # objective awaiting a human.
            from wax.objective.evidence import sync_awaiting_human_for_execution

            await sync_awaiting_human_for_execution(self._session, execution_id)
        self._services.metrics.approval_requested()
        return ApprovalGateOutcome(state=ApprovalGateState.PENDING, approval=record)

    # -------------------------------------------------------------------
    # Human notification — CV-14 fix: BOTH paths notify, and a
    # notification that cannot be delivered right now becomes durable
    # retry state (ADR-0021), not a silent log line.
    # -------------------------------------------------------------------

    async def _notify(self, record: Any) -> None:
        """Best-effort notify through every live channel; otherwise enqueue.

        The approval exists durably regardless. A notification that no
        live channel can carry becomes a pending DeliveryRecord the
        maintenance loop retries — the human-authority boundary may not
        depend on the interface adapter happening to be up.
        """
        from sqlalchemy import select

        from wax.identity.contracts import CREDENTIAL_KIND_INTERFACES
        from wax.state.identity_models import PrincipalCredential

        # CV-16 fix: no hardcoded credential-kind tuple here. The
        # principal's ACTUAL credentials decide the candidate channels;
        # the boundary table (single source of truth) maps credential
        # kind → interface. Attaching a new interface means adding one
        # mapping to `INTERFACE_CREDENTIAL_KINDS` — authority semantics
        # are never edited per interface.
        text = (
            f"Action requires your approval: {record.capability_name} "
            f"(approval {record.id}). Reply '/approve {record.id}' or "
            f"'/deny {record.id}'. Expires {record.expires_at.isoformat()}."
        )
        result = await self._session.execute(
            select(PrincipalCredential).where(
                PrincipalCredential.principal_id == record.principal_id
            )
        )
        fallback: tuple[str, str] | None = None  # (interface, recipient)
        for credential in result.scalars():
            interface = CREDENTIAL_KIND_INTERFACES.get(credential.kind)
            if interface is None:
                continue
            if fallback is None:
                fallback = (interface, credential.value)
            if not self._services.delivery.has(interface):
                continue  # adapter down — candidate for the durable retry
            try:
                await self._services.delivery.send(
                    interface, credential.value, text
                )
                log.info(
                    "approval.notified",
                    approval_id=record.id,
                    interface=interface,
                )
                return  # one live channel is enough
            except Exception as e:
                log.warning(
                    "approval.notification_failed",
                    approval_id=record.id,
                    interface=interface,
                    error=str(e)[:300],
                )

        # No live channel delivered. Enqueue durable retry state (the
        # DeliveryQueue sends through the same router when maintenance
        # runs, so the notification lands once an adapter is up).
        if fallback is not None:
            from wax.runtime.delivery_queue import DeliveryQueue

            settings = self._services.settings
            queue = DeliveryQueue(
                self._session,
                self._services,
                retry_backoff_seconds=float(settings.delivery_retry_backoff_seconds),
                max_age_seconds=float(settings.delivery_max_age_seconds),
            )
            delivery = await queue.enqueue(
                principal_id=record.principal_id,
                interface_kind=fallback[0],
                recipient_id=fallback[1],
                text=text,
                source="approval_notification",
                max_attempts=max(1, int(settings.delivery_max_attempts)),
            )
            log.warning(
                "approval.notification_enqueued",
                approval_id=record.id,
                delivery_id=delivery.id,
                interface=fallback[0],
            )
            return

        log.error(
            "approval.notification_impossible",
            approval_id=record.id,
            principal_id=record.principal_id,
            reason="principal has no interface credential on any known interface",
        )
