"""Runtime capabilities — capabilities over runtime mechanisms.

These are not features; they are the runtime's exposed mechanisms, the way
an OS exposes syscalls. The AI composes them to pursue objectives:

- work.schedule / work.cancel / work.list — durable work + time ownership
  (Phase R / V). A "reminder" is work.schedule(kind="capability",
  payload={capability_name: "message.send", ...}, wake_at=...).
- message.send — outbound delivery through the DeliveryRouter with Meta
  policy honesty (Phase W groundwork). The runtime knows the 24-hour
  customer-service window and refuses (truthfully) outside it; it never
  invents template capabilities that don't exist.

Every implementation receives an InvocationContext (principal_id, ...) —
authorization has ALREADY been enforced by the invoker before these run.
Implementations close over their OWN container (no module globals), so
multiple containers (tests, multi-runtime processes) stay isolated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from wax.runtime.services import RuntimeServices

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from wax.capabilities.contracts import CapabilityDescriptor, InvocationContext
from wax.capabilities.registry import CapabilityRegistry
from wax.objective.evidence import (
    sync_waiting_for_execution,
)
from wax.runtime.logging import get_logger
from wax.state.engine import db_session

log = get_logger(__name__)

# Interface kind → credential kind comes from the SINGLE source of truth in
# wax.identity.contracts. The capability layer owns no private copy of the
# identity boundary mapping, and no vendor delivery policy: an interface's
# own adapter declares its policy (see DeliveryRouter/DeliveryPolicy); the
# runtime only enforces whatever the attached interface declared.

MAX_WORK_DELAY = timedelta(days=30)


def _parse_wake_time(inputs: dict[str, Any]) -> datetime:
    """Resolve wake_at (ISO 8601) or delay_seconds from the inputs."""
    now = datetime.now(UTC)
    wake_at_raw = inputs.get("wake_at")
    delay_seconds = inputs.get("delay_seconds")

    if wake_at_raw is not None and delay_seconds is not None:
        raise ValueError("Provide either wake_at or delay_seconds, not both")

    if wake_at_raw is not None:
        if not isinstance(wake_at_raw, str):
            raise ValueError("wake_at must be an ISO 8601 string")
        try:
            wake_at = datetime.fromisoformat(wake_at_raw.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"wake_at is not valid ISO 8601: {e}") from e
        if wake_at.tzinfo is None:
            wake_at = wake_at.replace(tzinfo=UTC)
        return wake_at

    if delay_seconds is not None:
        try:
            delay = float(delay_seconds)
        except (TypeError, ValueError) as e:
            raise ValueError("delay_seconds must be a number") from e
        if delay < 0:
            raise ValueError("delay_seconds must be >= 0")
        if delay > MAX_WORK_DELAY.total_seconds():
            raise ValueError("delay_seconds exceeds the 30-day maximum")
        return now + timedelta(seconds=delay)

    raise ValueError("Provide wake_at (ISO 8601) or delay_seconds (number)")


def _parse_optional_deadline(inputs: dict[str, Any]) -> datetime | None:
    """Resolve the optional wait deadline: expires_at (ISO 8601) or
    expires_in_seconds. A waiting item whose condition is not met by the
    deadline dies honestly instead of waiting forever."""
    raw_at = inputs.get("expires_at")
    raw_in = inputs.get("expires_in_seconds")
    if raw_at is not None and raw_in is not None:
        raise ValueError("Provide either expires_at or expires_in_seconds, not both")
    if raw_at is None and raw_in is None:
        return None
    if raw_at is not None:
        if not isinstance(raw_at, str):
            raise ValueError("expires_at must be an ISO 8601 string")
        try:
            deadline = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"expires_at is not valid ISO 8601: {e}") from e
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return deadline
    try:
        seconds = float(raw_in)  # type: ignore[arg-type]
    except (TypeError, ValueError) as e:
        raise ValueError("expires_in_seconds must be a number") from e
    if seconds <= 0:
        raise ValueError("expires_in_seconds must be > 0")
    if seconds > MAX_WORK_DELAY.total_seconds():
        raise ValueError("expires_in_seconds exceeds the 30-day maximum")
    return datetime.now(UTC) + timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# Descriptor shapes (shared by registration; implementations are closures)
# ---------------------------------------------------------------------------

WORK_SCHEDULE_DESCRIPTOR = CapabilityDescriptor(
    name="work.schedule",
    description=(
        "Schedule durable work that survives restarts. Two wake shapes: "
        "TIME (wake_at or delay_seconds) or EVENT (wake_event: the name of "
        "a runtime signal to wait for, e.g. work.succeeded:<work_id> to run "
        "when that work finishes, or interface.message:<principal_id> to "
        "run when the user next messages). Optional deadline via "
        "expires_at/expires_in_seconds. payload.capability_name plus "
        "payload.inputs define WHAT runs on wake."
    ),
    version="2.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "description": "Must include capability_name; may include inputs",
            },
            "kind": {"type": "string", "default": "capability"},
            "wake_at": {"type": "string", "description": "ISO 8601 datetime (time wake)"},
            "delay_seconds": {"type": "number"},
            "wake_event": {
                "type": "string",
                "description": "Signal name to wait for (event wake). Mutually "
                "exclusive with wake_at/delay_seconds.",
            },
            "not_before": {
                "type": "string",
                "description": "ISO 8601; earliest claim time for event wakes",
            },
            "expires_at": {
                "type": "string",
                "description": "ISO 8601; if the condition is unmet by then the work dies honestly",
            },
            "expires_in_seconds": {"type": "number"},
            "max_attempts": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["payload"],
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

WORK_CANCEL_DESCRIPTOR = CapabilityDescriptor(
    name="work.cancel",
    description="Cancel the caller's own pending durable work by work_id.",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"work_id": {"type": "string"}},
        "required": ["work_id"],
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

WORK_LIST_DESCRIPTOR = CapabilityDescriptor(
    name="work.list",
    description="List the caller's own durable work items (optional status filter).",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"status": {"type": "string"}},
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

WORK_REQUEUE_DESCRIPTOR = CapabilityDescriptor(
    name="work.requeue",
    description=(
        "Requeue the caller's own DEAD work item (e.g. work that exhausted "
        "retries during a provider outage). Creates a fresh work item with "
        "the same payload, due immediately, provenance-linked to the dead "
        "item. The dead item is retained for audit."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"work_id": {"type": "string"}},
        "required": ["work_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "work_id": {"type": "string"},
            "requeued_from": {"type": "string"},
            "status": {"type": "string"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=False,
    is_destructive=False,
)

MESSAGE_SEND_DESCRIPTOR = CapabilityDescriptor(
    name="message.send",
    description=(
        "Send a text message to the requesting principal over one of THEIR "
        "attached interfaces. The interface is taken from interface_kind "
        "when given; otherwise it is derived from the requesting "
        "principal's verified credentials (refused loudly if that is "
        "ambiguous). Enforces identity ownership — the runtime never "
        "messages third parties — and enforces whatever delivery policy "
        "the attached interface itself declares (for example a vendor "
        "freshness window)."
    ),
    version="1.1.0",
    input_schema={
        "type": "object",
        "properties": {
            "recipient_id": {"type": "string"},
            "text": {
                "type": "string",
                "maxLength": 12000,
                "description": "Chunks over 4096 chars are delivered as marked parts",
            },
            "interface_kind": {
                "type": "string",
                "description": (
                    "Optional. Required explicitly only when the principal "
                    "has more than one attached interface."
                ),
            },
        },
        "required": ["recipient_id", "text"],
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=30.0,
    idempotent=False,
    is_destructive=False,
)


SCRATCH_WORKSPACE_DESCRIPTOR = CapabilityDescriptor(
    name="scratch.workspace",
    description=(
        "Provision a temporary scratch directory for the requesting "
        "principal. Ephemeral: it is destroyed automatically at TTL expiry. "
        "Use for intermediate files during multi-step work."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "ttl_seconds": {
                "type": "integer",
                "minimum": 10,
                "maximum": 86400,
                "default": 900,
            },
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=10.0,
    idempotent=False,
    is_destructive=False,
)


SIGNAL_EMIT_DESCRIPTOR = CapabilityDescriptor(
    name="signal.emit",
    description=(
        "Emit a named runtime signal onto the persistent event ledger. "
        "Waiting work whose wake_event matches wakes on the next runner "
        "pass. Runtime-owned namespaces (interface.*, work.*) cannot be "
        "emitted by the intelligence — only waited on."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Signal name, e.g. 'external.payment.received'",
                "maxLength": 160,
            },
            "payload": {
                "type": "object",
                "description": "Optional JSON evidence about the fact",
            },
        },
        "required": ["name"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "signal_id": {"type": "string"},
            "name": {"type": "string"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=False,
    is_destructive=False,
)


APPROVAL_LIST_DESCRIPTOR = CapabilityDescriptor(
    name="approval.list",
    description=(
        "List the requesting principal's human-approval requests (default: "
        "pending ones). Use this to check whether a requested authorization "
        "has been granted or denied, or to see what is waiting for the human."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["pending", "approved", "denied", "expired", "cancelled"],
                "description": "Filter by status (default: pending)",
            },
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

APPROVAL_CANCEL_DESCRIPTOR = CapabilityDescriptor(
    name="approval.cancel",
    description=(
        "Withdraw one of the requesting principal's own PENDING approval "
        "requests (e.g. the plan changed before the human decided). "
        "Approvals can never be granted by the intelligence — only "
        "cancelled."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"approval_id": {"type": "string"}},
        "required": ["approval_id"],
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=False,
    is_destructive=False,
)


def register_runtime_capabilities(registry: CapabilityRegistry, services: RuntimeServices) -> None:
    """Register the runtime-mechanism capabilities bound to THIS container."""

    # --- work.schedule ----------------------------------------------------

    async def work_schedule_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from wax.runtime.work.reentry import validate_reentry_payload
        from wax.runtime.work.repository import WorkRepository
        from wax.runtime.work.signals import (
            SignalNameError,
            validate_signal_name,
        )
        from wax.state.work_models import WAKE_KIND_EVENT, WAKE_KIND_TIME

        payload = inputs.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("payload object is required")

        kind = inputs.get("kind") or "capability"
        if kind not in ("capability", "intelligence"):
            raise ValueError(
                f"Unsupported work kind: {kind!r} (only 'capability' or 'intelligence')"
            )

        # ADR-0034: validate intelligence payload at SCHEDULE time too,
        # so a malformed work item cannot be persisted. The handler
        # re-validates at WAKE time (defense in depth against tampering).
        if kind == "intelligence":
            try:
                validate_reentry_payload(payload)
            except Exception as e:
                raise ValueError(f"Invalid intelligence work payload: {e}") from e
        else:
            # capability kind: payload must contain a capability_name
            capability_name = payload.get("capability_name")
            if not capability_name or not isinstance(capability_name, str):
                raise ValueError("payload.capability_name is required")
            # Honest early feedback: refuse to schedule work whose capability
            # does not exist (availability may still change by wake time).
            try:
                descriptor, _impl = services.capability_registry.get(capability_name)
            except Exception as e:
                raise ValueError(f"Unknown capability for scheduled work: {capability_name}") from e

            if descriptor.is_destructive:
                # Destructive work is schedulable: at wake time the agency gate
                # routes it into the human-approval primitive (a pending
                # approval is created honestly, the attempt fails without
                # consuming anything, and the human's approval authorizes the
                # retry/requeue exactly once). The authority boundary moved
                # from "refuse to schedule" to "refuse to run without an
                # explicit human YES" — the stronger, generic guarantee.
                pass

        # Wake condition: TIME (wake_at/delay_seconds) or EVENT (wake_event).
        wake_event_raw = inputs.get("wake_event")
        has_time = inputs.get("wake_at") is not None or inputs.get("delay_seconds") is not None
        if wake_event_raw is not None and has_time:
            raise ValueError(
                "wake_event is mutually exclusive with wake_at/delay_seconds: "
                "wait for a condition OR a time, not both"
            )
        try:
            max_attempts = int(inputs.get("max_attempts", 3))
        except (TypeError, ValueError) as e:
            raise ValueError("max_attempts must be an integer") from e
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be between 1 and 10")

        expires_at = _parse_optional_deadline(inputs)

        if wake_event_raw is not None:
            # Event wake. The intelligence may wait on ANY signal name —
            # including runtime-owned ones (work.succeeded:<id>,
            # interface.message:<principal>) — but only the runtime can
            # EMIT those. Validation is format-only here.
            try:
                wake_event = validate_signal_name(str(wake_event_raw), allow_reserved=True)
            except SignalNameError as e:
                raise ValueError(str(e)) from e
            wake_kind = WAKE_KIND_EVENT
            # Floor: not_before if given, else now (claimable as soon as
            # the condition is met).
            if inputs.get("not_before") is not None:
                raw = str(inputs["not_before"])
                try:
                    wake_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError as e:
                    raise ValueError(f"not_before is not valid ISO 8601: {e}") from e
                if wake_at.tzinfo is None:
                    wake_at = wake_at.replace(tzinfo=UTC)
            else:
                wake_at = datetime.now(UTC)
            if expires_at is not None and expires_at <= wake_at:
                raise ValueError("expires_at must be after not_before")
        else:
            wake_kind = WAKE_KIND_TIME
            wake_event = None
            wake_at = _parse_wake_time(inputs)

        # For capability kind, normalize the payload to the persisted shape.
        if kind == "capability":
            persisted_payload = {
                "capability_name": payload.get("capability_name"),
                "inputs": payload.get("inputs") or {},
            }
        else:
            # For intelligence kind, persist the bounded payload exactly
            # as validated (prompt + observation). The handler will
            # re-validate at wake time.
            persisted_payload = payload

        async with db_session() as session:
            repo = WorkRepository(session)
            item = await repo.schedule(
                kind=kind,
                payload=persisted_payload,
                wake_at=wake_at,
                principal_id=ctx.principal_id,
                # Traceability: link the work back to WHO called for it.
                # On the live bridge path ctx.request_id IS the bridge
                # execution id (execution → objective); from durable work
                # it is the parent work item. The invoker-internal
                # execution id is the fallback for direct calls.
                execution_id=ctx.request_id or ctx.execution_id or None,
                max_attempts=max_attempts,
                wake_kind=wake_kind,
                wake_event=wake_event,
                expires_at=expires_at,
            )
            # Evidence sync (ADR-0020): scheduled durable work under this
            # execution means the objective IS waiting on a real condition.
            await sync_waiting_for_execution(session, ctx.request_id or ctx.execution_id)
            await session.commit()

        return {
            "work_id": item.id,
            "status": item.status,
            "wake_kind": item.wake_kind,
            "wake_event": item.wake_event,
            "wake_at": item.wake_at.isoformat(),
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "kind": kind,
            "capability_name": (payload.get("capability_name") if kind == "capability" else None),
        }

    # --- work.cancel ------------------------------------------------------

    async def work_cancel_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from wax.runtime.work.repository import TERMINAL_WORK_STATUSES, WorkRepository

        work_id = inputs.get("work_id")
        if not work_id or not isinstance(work_id, str):
            raise ValueError("work_id is required")

        async with db_session() as session:
            repo = WorkRepository(session)
            item = await repo.get(work_id)
            if item is None:
                raise ValueError(f"No such work item: {work_id}")
            if item.principal_id != ctx.principal_id:
                # Ownership boundary: only the owning human's AI may cancel.
                raise ValueError("work_id belongs to a different principal")
            if item.status in TERMINAL_WORK_STATUSES:
                return {
                    "work_id": work_id,
                    "status": item.status,
                    "cancelled": False,
                }
            new_status = await repo.cancel(work_id)
            await session.commit()

        return {"work_id": work_id, "status": new_status, "cancelled": True}

    # --- work.list --------------------------------------------------------

    async def work_list_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from wax.runtime.work.repository import VALID_WORK_STATUSES, WorkRepository

        status = inputs.get("status")
        if status is not None and status not in VALID_WORK_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_WORK_STATUSES)}")

        async with db_session() as session:
            repo = WorkRepository(session)
            items = await repo.list_for_principal(ctx.principal_id, status=status, limit=20)

        return {
            "count": len(items),
            "items": [
                {
                    "work_id": i.id,
                    "kind": i.kind,
                    "status": i.status,
                    "wake_kind": i.wake_kind,
                    "wake_event": i.wake_event,
                    "wake_at": i.wake_at.isoformat(),
                    "expires_at": i.expires_at.isoformat() if i.expires_at else None,
                    "attempts": i.attempts,
                    "last_error": (i.last_error or "")[:200] or None,
                    "payload": i.payload,
                }
                for i in items
            ],
        }

    # --- message.send -----------------------------------------------------

    async def message_send_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from wax.identity.contracts import (
            CREDENTIAL_KIND_INTERFACES,
            INTERFACE_CREDENTIAL_KINDS,
        )
        from wax.state.bridge_models import ProcessedMessageRecord
        from wax.state.identity_models import PrincipalCredential

        recipient_id = inputs.get("recipient_id")
        text = inputs.get("text")
        if not recipient_id or not isinstance(recipient_id, str):
            raise ValueError("recipient_id is required")
        if not text or not isinstance(text, str):
            raise ValueError("text is required")
        if len(text) > 12000:
            raise ValueError("text exceeds 12000 characters")

        delivery = services.delivery

        async with db_session() as session:
            # 0. Interface selection — identity-derived, never a hardcoded
            # default. Explicit interface_kind wins; otherwise the
            # requesting principal's verified credentials decide. If that
            # is ambiguous (several attached interfaces), the runtime
            # refuses loudly instead of guessing.
            interface_input = inputs.get("interface_kind")
            if interface_input:
                interface_kind = str(interface_input)
                if interface_kind not in INTERFACE_CREDENTIAL_KINDS:
                    raise ValueError(f"Unknown interface kind: {interface_kind!r}")
            else:
                rows = await session.execute(
                    select(PrincipalCredential.kind).where(
                        PrincipalCredential.principal_id == ctx.principal_id
                    )
                )
                attached = sorted(
                    {
                        CREDENTIAL_KIND_INTERFACES[kind]
                        for (kind,) in rows.all()
                        if kind in CREDENTIAL_KIND_INTERFACES
                        and delivery.has(CREDENTIAL_KIND_INTERFACES[kind])
                    }
                )
                if not attached:
                    raise ValueError(
                        "The requesting principal has no verified credential on "
                        "any attached delivery interface; the runtime cannot "
                        "send this message (an honest constraint, not a "
                        "success)."
                    )
                if len(attached) > 1:
                    raise ValueError(
                        "The requesting principal has several attached "
                        f"interfaces ({', '.join(attached)}); provide "
                        "interface_kind explicitly."
                    )
                interface_kind = attached[0]
            credential_kind = INTERFACE_CREDENTIAL_KINDS[interface_kind]

            if not delivery.has(interface_kind):
                raise ValueError(
                    f"No delivery interface attached for {interface_kind!r}; the "
                    "runtime cannot send messages right now (an honest "
                    "constraint, not a success)."
                )

            # 1. Ownership: recipient must be the CALLING principal's own
            # verified credential. The AI cannot message third parties.
            result = await session.execute(
                select(PrincipalCredential).where(
                    PrincipalCredential.kind == credential_kind,
                    PrincipalCredential.value == recipient_id,
                )
            )
            credential = result.scalar_one_or_none()
            if credential is None or credential.principal_id != ctx.principal_id:
                raise ValueError(
                    "recipient_id is not a verified identity of the requesting "
                    "principal; WAX will not message third parties"
                )

            # 2. Interface-declared delivery policy — the runtime enforces
            # whatever the attached interface declared, generically. It
            # invents no vendor rule of its own.
            policy = delivery.policy_for(interface_kind)
            if policy is not None and policy.inbound_freshness_window is not None:
                recent = await session.execute(
                    select(ProcessedMessageRecord.received_at)
                    .where(ProcessedMessageRecord.principal_id == ctx.principal_id)
                    .order_by(ProcessedMessageRecord.received_at.desc())
                    .limit(1)
                )
                last_inbound = recent.scalar_one_or_none()
                if last_inbound is not None and last_inbound.tzinfo is None:
                    last_inbound = last_inbound.replace(tzinfo=UTC)
                now = datetime.now(UTC)
                if last_inbound is None or (now - last_inbound) > policy.inbound_freshness_window:
                    note = policy.freshness_note or (
                        "the attached interface does not accept a plain-text delivery right now"
                    )
                    raise ValueError(
                        f"Delivery refused by the attached {interface_kind} "
                        f"interface's delivery policy: {note}"
                    )

        try:
            await delivery.send(interface_kind, recipient_id, text)
        except Exception as send_error:
            # CV-13 fix: a failed send is recoverable delivery state, not
            # a dead letter (ADR-0021, mission §55). The runtime now OWES
            # this message: it lands on the DeliveryQueue and the
            # maintenance loop retries it with backoff until delivered,
            # exhausted, or past the deliverability horizon. The result
            # reports the honest state — nothing was delivered YET.
            from wax.runtime.delivery_queue import DeliveryQueue

            services.metrics.send_failure(interface_kind)
            async with db_session() as retry_session:
                queue = DeliveryQueue(
                    retry_session,
                    services,
                    retry_backoff_seconds=float(services.settings.delivery_retry_backoff_seconds),
                    max_age_seconds=float(services.settings.delivery_max_age_seconds),
                )
                delivery_record = await queue.enqueue(
                    principal_id=ctx.principal_id,
                    interface_kind=interface_kind,
                    recipient_id=recipient_id,
                    text=text,
                    source="capability:message.send",
                    execution_id=ctx.execution_id,
                    max_attempts=max(1, int(services.settings.delivery_max_attempts)),
                )
                # One attempt already happened (this failed send) — the
                # backoff chain starts honestly from attempt 1.
                delivery_record.attempts = 1
                delivery_record.last_error = (f"{type(send_error).__name__}: {send_error}")[:2000]
                delivery_record.next_attempt_at = datetime.now(UTC) + timedelta(
                    seconds=float(services.settings.delivery_retry_backoff_seconds)
                )
                await retry_session.commit()
            log.warning(
                "message.send.queued_for_retry",
                delivery_id=delivery_record.id,
                interface=interface_kind,
                principal_id=ctx.principal_id,
                error=str(send_error)[:300],
            )
            return {
                "sent": False,
                "queued_for_retry": True,
                "delivery_id": delivery_record.id,
                "interface": interface_kind,
                "recipient_id": recipient_id,
                "error": f"{type(send_error).__name__}: {send_error}"[:200],
            }
        services.metrics.send_ok(interface_kind)
        return {
            "sent": True,
            "interface": interface_kind,
            "recipient_id": recipient_id,
        }

    # --- work.requeue: dead work is a recovery state, not a graveyard ----

    async def work_requeue_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from wax.runtime.work.repository import WorkRepository

        work_id = inputs.get("work_id")
        if not work_id or not isinstance(work_id, str):
            raise ValueError("work_id is required")

        async with db_session() as session:
            repo = WorkRepository(session)
            item = await repo.get(work_id)
            if item is None:
                raise ValueError(f"No such work item: {work_id}")
            if item.principal_id != ctx.principal_id:
                raise ValueError("work_id belongs to a different principal")
            if item.status != "dead":
                # Honest constraint: requeue is the recovery path for DEAD
                # work only — live work retries on its own; terminal success
                # must not be double-run.
                raise ValueError(f"Only dead work can be requeued (status={item.status})")
            payload = dict(item.payload or {})
            payload["requeued_from"] = item.id
            # The requeued item runs as a FRESH attempt, due now, with the
            # same handler payload and the same owner. The original stays
            # dead for audit; the new item carries the provenance link.
            new_item = await repo.schedule(
                kind=item.kind,
                payload=payload,
                wake_at=datetime.now(UTC),
                principal_id=item.principal_id,
                execution_id=item.execution_id,
                max_attempts=item.max_attempts,
            )
            await session.commit()

        return {
            "work_id": new_item.id,
            "requeued_from": item.id,
            "status": new_item.status,
        }

    # --- signal.emit: gated emission onto the runtime event ledger ------

    async def signal_emit_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from wax.runtime.work.signals import (
            SignalNameError,
            SignalRepository,
            validate_signal_name,
        )

        name_raw = inputs.get("name")
        if not name_raw or not isinstance(name_raw, str):
            raise ValueError("name is required")
        try:
            # The runtime owns interface.*/work.* signals: the intelligence
            # may WAIT on "the user replied" or "that work finished", but
            # emitting those facts itself would forge runtime boundaries.
            name = validate_signal_name(name_raw, allow_reserved=False)
        except SignalNameError as e:
            raise ValueError(str(e)) from e

        signal_payload = inputs.get("payload")
        if signal_payload is not None and not isinstance(signal_payload, dict):
            raise ValueError("payload must be an object")
        if signal_payload is not None and len(str(signal_payload)) > 8000:
            raise ValueError("payload is too large (8000 chars serialized max)")

        async with db_session() as session:
            record = await SignalRepository(session).emit(
                name,
                payload=signal_payload,
                emitted_by=f"principal:{ctx.principal_id}",
            )
            await session.commit()

        services.metrics.signal_emitted(name)
        return {
            "signal_id": record.id,
            "name": record.name,
            "emitted_at": record.emitted_at.isoformat(),
        }

    # --- scratch.workspace (Phase S) ----------------------------------

    async def scratch_workspace_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from wax.runtime.provisioning import (
            ProvisioningError,
            ProvisioningService,
        )

        ttl_seconds = inputs.get("ttl_seconds", 900)
        try:
            ttl_seconds = int(ttl_seconds)
        except (TypeError, ValueError) as e:
            raise ValueError("ttl_seconds must be an integer") from e

        provisioning = ProvisioningService(services.settings)
        try:
            async with db_session() as session:
                record = await provisioning.provision_scratch_dir(
                    session,
                    principal_id=ctx.principal_id,
                    ttl_seconds=ttl_seconds,
                    execution_id=ctx.execution_id,
                )
                await session.commit()
        except ProvisioningError as e:
            raise ValueError(str(e)) from e

        services.metrics.resource_provisioned("scratch_dir")
        return {
            "resource_id": record.id,
            "kind": "scratch_dir",
            # P0-12: Do NOT return the absolute host path (record.uri).
            # The intelligence receives only the opaque resource_id; the
            # host filesystem layout is an implementation detail.
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        }

    # --- code.run (Phase U): isolated execution behind explicit authority.
    # Registered here so it ships with the container; the member role does
    # NOT include capability.invoke:code_run, so it is denied for ordinary
    # principals until a deployment grants it (trust boundary by default).
    from wax.capabilities.code_run import register_code_run_capability

    register_code_run_capability(registry, services)

    # --- workspace.acquire: artifact acquisition into isolated workspaces
    # (integrity-verified, allowlisted, cached, audited — ADR-0016).
    from wax.capabilities.workspace_acquire import register_workspace_acquire_capability

    register_workspace_acquire_capability(registry, services)

    # --- approval surface (the AI can inspect/cancel, never decide) ----

    async def approval_list_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from wax.authority.approvals import ApprovalService

        status = inputs.get("status") or "pending"
        async with db_session() as session:
            records = await ApprovalService(session).list_for_principal(
                ctx.principal_id, status=status, limit=20
            )
            await session.commit()
        return {
            "status_filter": status,
            "approvals": [
                {
                    "approval_id": r.id,
                    "capability": r.capability_name,
                    "status": r.status,
                    "requested_at": r.requested_at.isoformat(),
                    "expires_at": r.expires_at.isoformat(),
                    "scope": r.scope_summary or {},
                }
                for r in records
            ],
        }

    async def approval_cancel_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from wax.authority.approvals import ApprovalDecisionError, ApprovalService

        approval_id = inputs.get("approval_id")
        if not approval_id or not isinstance(approval_id, str):
            raise ValueError("approval_id is required")
        async with db_session() as session:
            try:
                await ApprovalService(session).cancel(approval_id, by_principal_id=ctx.principal_id)
            except ApprovalDecisionError as e:
                await session.rollback()
                raise ValueError(str(e)) from e
            await session.commit()
        return {"approval_id": approval_id, "status": "cancelled"}

    registry.register(WORK_SCHEDULE_DESCRIPTOR, work_schedule_impl)
    registry.register(WORK_CANCEL_DESCRIPTOR, work_cancel_impl)
    registry.register(WORK_LIST_DESCRIPTOR, work_list_impl)
    registry.register(WORK_REQUEUE_DESCRIPTOR, work_requeue_impl)
    registry.register(MESSAGE_SEND_DESCRIPTOR, message_send_impl)
    registry.register(SCRATCH_WORKSPACE_DESCRIPTOR, scratch_workspace_impl)
    registry.register(SIGNAL_EMIT_DESCRIPTOR, signal_emit_impl)
    registry.register(APPROVAL_LIST_DESCRIPTOR, approval_list_impl)
    registry.register(APPROVAL_CANCEL_DESCRIPTOR, approval_cancel_impl)
    registry.register(MEMORY_STORE_DESCRIPTOR, memory_store_impl)
    registry.register(MEMORY_SEARCH_DESCRIPTOR, memory_search_impl)
    registry.register(MEMORY_FORGET_DESCRIPTOR, memory_forget_impl)
    registry.register(MEMORY_CONSOLIDATE_DESCRIPTOR, memory_consolidate_impl)
    registry.register(OBJECTIVE_LIST_DESCRIPTOR, objective_list_impl)
    registry.register(OBJECTIVE_RESUME_DESCRIPTOR, objective_resume_impl)
    registry.register(OBJECTIVE_UPDATE_STATUS_DESCRIPTOR, objective_update_status_impl)
    registry.register(MEMORY_LINK_DESCRIPTOR, memory_link_impl)

    # ========================================================================
    # ADR-0038 (Phase 5): Environment Negotiation
    # ========================================================================

    async def environment_request_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        """Plan + provision an environment lease."""
        from wax.runtime.environment.contracts import (
            EnvironmentValidationError,
            validate_environment_requirement,
        )
        from wax.state.engine import db_session

        try:
            validate_environment_requirement(inputs)
        except EnvironmentValidationError as e:
            raise ValueError(f"Invalid environment requirement: {e}") from e

        if services.environment_planner is None:
            raise ValueError(
                "Environment planner not configured in this runtime "
                "(services.environment_planner is None)"
            )

        ttl_seconds = inputs.get("ttl_seconds")
        if ttl_seconds is not None:
            ttl_seconds = int(ttl_seconds)

        async with db_session() as session:
            try:
                plan = await services.environment_planner.plan(
                    session,
                    principal_id=ctx.principal_id,
                    execution_id=ctx.request_id or ctx.execution_id,
                    requirement_dict=inputs,
                )
                lease = await services.environment_planner.provision_lease(
                    session,
                    principal_id=ctx.principal_id,
                    execution_id=ctx.request_id or ctx.execution_id,
                    plan=plan,
                    ttl_seconds=ttl_seconds,
                )
                await session.commit()
            except EnvironmentValidationError as e:
                raise ValueError(f"Environment validation failed: {e}") from e

        return {
            "environment_id": lease.environment_id,
            "expires_at": lease.expires_at.isoformat(),
            "state": lease.state.value,
            "degraded": lease.plan.degraded,
            "workspace_id": lease.plan.workspace_id,
            "credential_handles": lease.plan.credential_handles,
            "notes": lease.plan.notes,
        }

    registry.register(ENVIRONMENT_REQUEST_DESCRIPTOR, environment_request_impl)

    # ========================================================================
    # ADR-0039 (Phase 6): Terminal Runtime
    # ========================================================================

    async def terminal_session_open_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from datetime import UTC, datetime, timedelta

        from ulid import ULID

        from wax.state.engine import db_session
        from wax.state.environment_models import EnvironmentLeaseRecord
        from wax.state.terminal_models import TerminalSessionRecord

        environment_id = inputs.get("environment_id")
        if not isinstance(environment_id, str) or not environment_id:
            raise ValueError("environment_id is required")
        working_dir = inputs.get("working_dir", ".")
        if not isinstance(working_dir, str) or not working_dir:
            raise ValueError("working_dir must be a string")
        # Reject absolute paths — working_dir is workspace-relative
        if working_dir.startswith("/"):
            raise ValueError("working_dir must be workspace-relative (no absolute paths)")
        env_vars = inputs.get("env_vars", {})
        if not isinstance(env_vars, dict):
            raise ValueError("env_vars must be an object")
        if len(env_vars) > 50:
            raise ValueError("env_vars exceeds 50 entries")
        # Validate env var values — reject secret-like patterns
        for k, v in env_vars.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("env_vars keys and values must be strings")
            if len(v) > 4096:
                raise ValueError(f"env_var {k} value exceeds 4096 chars")
            kl = k.lower()
            if "token" in kl or "secret" in kl or "password" in kl or "api_key" in kl:
                raise ValueError(
                    f"env_var {k} looks like a secret; secrets must not enter intelligence"
                )

        ttl_seconds = inputs.get("ttl_seconds", 3600)
        try:
            ttl_seconds = int(ttl_seconds)
        except (TypeError, ValueError) as e:
            raise ValueError("ttl_seconds must be an integer") from e
        if not 1 <= ttl_seconds <= 86400:
            raise ValueError("ttl_seconds must be between 1 and 86400")

        async with db_session() as session:
            # Verify the environment exists + is active
            lease = await session.get(EnvironmentLeaseRecord, environment_id)
            if lease is None:
                raise ValueError(f"No such environment: {environment_id}")
            if lease.status not in ("planned", "provisioned", "active"):
                raise ValueError(
                    f"Environment {environment_id} is {lease.status}; cannot open session"
                )
            if lease.principal_id != ctx.principal_id:
                raise ValueError("environment belongs to a different principal")

            session_record = TerminalSessionRecord(
                id=str(ULID()),
                principal_id=ctx.principal_id,
                execution_id=ctx.request_id or ctx.execution_id,
                environment_id=environment_id,
                status="active",
                working_dir=working_dir,
                env_vars=env_vars,
                expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
            )
            session.add(session_record)
            await session.flush()
            await session.commit()
            sid = session_record.id
            expires = session_record.expires_at

        return {
            "session_id": sid,
            "state": "active",
            "expires_at": expires.isoformat() if expires else None,
        }

    async def terminal_execute_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        import asyncio
        import os
        from datetime import UTC, datetime

        from wax.state.engine import db_session
        from wax.state.terminal_models import TerminalSessionRecord

        session_id = inputs.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")
        command = inputs.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("command must be a non-empty string")
        if len(command) > 8192:
            raise ValueError("command exceeds 8192 chars")
        timeout_seconds = inputs.get("timeout_seconds", 30)
        try:
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError) as e:
            raise ValueError("timeout_seconds must be a number") from e
        if not 1 <= timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be between 1 and 600")
        max_output_bytes = int(inputs.get("max_output_bytes", 65536))
        if not 1024 <= max_output_bytes <= 1_048_576:
            raise ValueError("max_output_bytes must be between 1024 and 1048576")

        async with db_session() as session:
            record = await session.get(TerminalSessionRecord, session_id)
            if record is None:
                raise ValueError(f"No such terminal session: {session_id}")
            if record.principal_id != ctx.principal_id:
                raise ValueError("session belongs to a different principal")
            if record.status != "active":
                raise ValueError(f"session is {record.status}; cannot execute")
            # Check TTL
            if record.expires_at is not None:
                expires = record.expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                if datetime.now(UTC) > expires:
                    record.status = "expired"
                    await session.commit()
                    raise ValueError("session has expired")

            # Execute the command in a subprocess with process group
            # (governed by the session's environment lease — workspace +
            # isolation boundary are inherited from the environment).
            # SECURITY (P0-3): DO NOT inherit os.environ
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/tmp",
                "LANG": "en_US.UTF-8",
            }
            env.update(record.env_vars or {})

            # Resolve the workspace path for cwd
            cwd = None
            if record.environment_id:
                from wax.state.environment_models import EnvironmentLeaseRecord
                lease = await session.get(EnvironmentLeaseRecord, record.environment_id)
                if lease and lease.workspace_resource_id:
                    from wax.state.provisioning_models import ProvisionedResourceRecord
                    ws = await session.get(ProvisionedResourceRecord, lease.workspace_resource_id)
                    if ws and ws.uri:
                        cwd = ws.uri

            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                    cwd=cwd,
                    preexec_fn=os.setsid if hasattr(os, "setsid") else None,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=timeout_seconds
                    )
                    timed_out = False
                except TimeoutError:
                    # Kill the entire process group
                    if hasattr(os, "killpg"):
                        import contextlib

                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(os.getpgid(proc.pid), 9)
                    await proc.wait()
                    stdout = b""
                    stderr = b"command timed out"
                    timed_out = True

                # Truncate output
                stdout_str = stdout.decode("utf-8", errors="replace")[:max_output_bytes]
                stderr_str = stderr.decode("utf-8", errors="replace")[:max_output_bytes]
                truncated = len(stdout) > max_output_bytes or len(stderr) > max_output_bytes

                record.last_command_at = datetime.now(UTC)
                record.last_exit_code = proc.returncode if proc.returncode is not None else -1
                await session.commit()

                return {
                    "exit_code": record.last_exit_code,
                    "stdout": stdout_str,
                    "stderr": stderr_str,
                    "timed_out": timed_out,
                    "truncated": truncated,
                    "duration_ms": 0,  # not measured here for simplicity
                }
            except Exception as e:
                record.status = "failed"
                record.error = f"execution error: {e}"
                await session.commit()
                raise ValueError(f"terminal execution failed: {e}") from e

    async def terminal_session_close_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:

        from wax.state.engine import db_session
        from wax.state.terminal_models import TerminalSessionRecord

        session_id = inputs.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id is required")

        async with db_session() as session:
            record = await session.get(TerminalSessionRecord, session_id)
            if record is None:
                raise ValueError(f"No such terminal session: {session_id}")
            if record.principal_id != ctx.principal_id:
                raise ValueError("session belongs to a different principal")
            if record.status == "closed":
                return {"session_id": session_id, "closed": False, "state": "closed"}
            record.status = "closed"
            await session.commit()

        return {"session_id": session_id, "closed": True, "state": "closed"}

    registry.register(TERMINAL_SESSION_OPEN_DESCRIPTOR, terminal_session_open_impl)
    registry.register(TERMINAL_EXECUTE_DESCRIPTOR, terminal_execute_impl)
    registry.register(TERMINAL_SESSION_CLOSE_DESCRIPTOR, terminal_session_close_impl)

    # ========================================================================
    # ADR-0040 (Phase 7): Credential Vault
    # ========================================================================

    async def credential_connect_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from wax.state.engine import db_session

        connector_name = inputs.get("connector")
        if not isinstance(connector_name, str) or not connector_name:
            raise ValueError("connector is required")
        secret = inputs.get("secret")
        if not isinstance(secret, str) or not secret:
            raise ValueError("secret is required")
        if len(secret) > 8192:
            raise ValueError("secret exceeds 8192 chars")
        scopes = inputs.get("scopes", [])
        if not isinstance(scopes, list):
            raise ValueError("scopes must be a list")

        if services.credential_vault is None:
            raise ValueError("credential vault not configured")

        async with db_session() as session:
            try:
                connection_id = await services.credential_vault.connect(
                    session,
                    principal_id=ctx.principal_id,
                    connector_name=connector_name,
                    secret=secret,
                    scopes=scopes,
                )
                await session.commit()
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"credential connect failed: {e}") from e

        return {"connection_id": connection_id, "connector": connector_name, "scopes": scopes}

    async def credential_request_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from wax.state.engine import db_session

        connection_id = inputs.get("connection_id")
        if not isinstance(connection_id, str) or not connection_id:
            raise ValueError("connection_id is required")
        scopes = inputs.get("scopes", [])
        if not isinstance(scopes, list):
            raise ValueError("scopes must be a list")
        purpose = inputs.get("purpose")
        if purpose is not None and not isinstance(purpose, str):
            raise ValueError("purpose must be a string")
        if purpose and len(purpose) > 500:
            raise ValueError("purpose exceeds 500 chars")
        ttl_seconds = inputs.get("ttl_seconds", 3600)
        try:
            ttl_seconds = int(ttl_seconds)
        except (TypeError, ValueError) as e:
            raise ValueError("ttl_seconds must be an integer") from e
        if not 1 <= ttl_seconds <= 86400:
            raise ValueError("ttl_seconds must be between 1 and 86400")

        if services.credential_vault is None:
            raise ValueError("credential vault not configured")

        async with db_session() as session:
            try:
                grant = await services.credential_vault.request_grant(
                    session,
                    principal_id=ctx.principal_id,
                    connection_id=connection_id,
                    scopes=scopes,
                    purpose=purpose,
                    objective_id=inputs.get("objective_id"),
                    execution_id=ctx.request_id or ctx.execution_id,
                    ttl_seconds=ttl_seconds,
                )
                await session.commit()
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"credential request failed: {e}") from e

        return grant

    async def credential_list_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from wax.state.engine import db_session

        if services.credential_vault is None:
            raise ValueError("credential vault not configured")

        async with db_session() as session:
            connections = await services.credential_vault.list_connections(
                session, principal_id=ctx.principal_id
            )
            await session.commit()

        return {"connections": connections, "count": len(connections)}

    async def credential_revoke_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from wax.state.engine import db_session

        connection_id = inputs.get("connection_id")
        grant_id = inputs.get("grant_id")
        if not connection_id and not grant_id:
            raise ValueError("either connection_id or grant_id is required")
        reason = inputs.get("reason")

        if services.credential_vault is None:
            raise ValueError("credential vault not configured")

        async with db_session() as session:
            try:
                result = await services.credential_vault.revoke(
                    session,
                    principal_id=ctx.principal_id,
                    connection_id=connection_id,
                    grant_id=grant_id,
                    reason=reason,
                )
                await session.commit()
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"credential revoke failed: {e}") from e

        return result

    registry.register(CREDENTIAL_CONNECT_DESCRIPTOR, credential_connect_impl)
    registry.register(CREDENTIAL_REQUEST_DESCRIPTOR, credential_request_impl)
    registry.register(CREDENTIAL_LIST_DESCRIPTOR, credential_list_impl)
    registry.register(CREDENTIAL_REVOKE_DESCRIPTOR, credential_revoke_impl)

    # ========================================================================
    # ADR-0041 (Phase 8): Generic Connector Runtime
    # ========================================================================

    async def connector_discover_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from wax.state.engine import db_session

        if services.connector_runtime is None:
            raise ValueError("connector runtime not configured")

        connector_filter = inputs.get("connector")
        if connector_filter is not None and (
            not isinstance(connector_filter, str) or not connector_filter
        ):
            raise ValueError("connector must be a non-empty string")

        async with db_session() as session:
            connectors = await services.connector_runtime.discover(
                session, connector_filter=connector_filter
            )
            await session.commit()

        return {"connectors": connectors, "count": len(connectors)}

    async def connector_resolve_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from wax.state.engine import db_session

        grant_handle = inputs.get("grant_handle")
        if not isinstance(grant_handle, str) or not grant_handle:
            raise ValueError("grant_handle is required")

        if services.connector_runtime is None:
            raise ValueError("connector runtime not configured")

        async with db_session() as session:
            try:
                binding = await services.connector_runtime.resolve(
                    session,
                    grant_handle=grant_handle,
                    principal_id=ctx.principal_id,
                )
                await session.commit()
            except ValueError:
                raise
            except Exception as e:
                raise ValueError(f"connector resolve failed: {e}") from e

        return binding

    registry.register(CONNECTOR_DISCOVER_DESCRIPTOR, connector_discover_impl)
    registry.register(CONNECTOR_RESOLVE_DESCRIPTOR, connector_resolve_impl)

    # ========================================================================
    # ADR-0042 (Phase 9): Workspace + Artifact Lifecycle
    # ========================================================================

    async def workspace_snapshot_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        import hashlib
        import os
        from datetime import UTC, datetime
        from pathlib import Path

        from ulid import ULID

        from wax.state.engine import db_session
        from wax.state.provisioning_models import ProvisionedResourceRecord
        from wax.state.workspace_models import WorkspaceSnapshotRecord

        workspace_id = inputs.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id is required")

        async with db_session() as session:
            resource = await session.get(ProvisionedResourceRecord, workspace_id)
            if resource is None:
                raise ValueError(f"No such workspace: {workspace_id}")
            if resource.principal_id != ctx.principal_id:
                raise ValueError("workspace belongs to a different principal")
            if resource.status != "active":
                raise ValueError(f"workspace is {resource.status}")

            # Walk the workspace directory
            workspace_path = Path(resource.uri)
            if not workspace_path.exists():
                raise ValueError(f"workspace path does not exist: {workspace_id}")

            files: list[dict[str, Any]] = []
            total_bytes = 0
            for root, _dirs, filenames in os.walk(workspace_path):
                for fname in filenames:
                    fpath = Path(root) / fname
                    rel_path = str(fpath.relative_to(workspace_path))
                    try:
                        size = fpath.stat().st_size
                        with open(fpath, "rb") as f:
                            sha = hashlib.sha256(f.read()).hexdigest()
                        files.append({"path": rel_path, "sha256": sha, "size": size})
                        total_bytes += size
                    except OSError:
                        pass  # skip unreadable files

            # Content-addressed hash
            content_hash = hashlib.sha256(
                "\n".join(
                    f["path"] + f["sha256"] for f in sorted(files, key=lambda x: x["path"])
                ).encode()
            ).hexdigest()

            # Check for an existing snapshot with the same content_hash (idempotent)
            existing = (
                await session.execute(
                    select(WorkspaceSnapshotRecord).where(
                        WorkspaceSnapshotRecord.workspace_resource_id == workspace_id,
                        WorkspaceSnapshotRecord.content_hash == content_hash,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                await session.commit()
                return {
                    "snapshot_id": existing.id,
                    "file_count": existing.file_count,
                    "total_bytes": existing.total_bytes,
                    "files": existing.files_json,
                    "idempotent": True,
                }

            snapshot = WorkspaceSnapshotRecord(
                id=str(ULID()),
                principal_id=ctx.principal_id,
                workspace_resource_id=workspace_id,
                execution_id=ctx.request_id or ctx.execution_id,
                content_hash=content_hash,
                files_json=files,
                file_count=len(files),
                total_bytes=total_bytes,
                captured_at=datetime.now(UTC),
            )
            session.add(snapshot)
            await session.flush()
            await session.commit()

            return {
                "snapshot_id": snapshot.id,
                "file_count": snapshot.file_count,
                "total_bytes": snapshot.total_bytes,
                "files": snapshot.files_json,
                "idempotent": False,
            }

    async def workspace_restore_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from pathlib import Path

        from wax.state.engine import db_session
        from wax.state.provisioning_models import ProvisionedResourceRecord
        from wax.state.workspace_models import WorkspaceSnapshotRecord

        snapshot_id = inputs.get("snapshot_id")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("snapshot_id is required")
        target_workspace_id = inputs.get("target_workspace_id")
        if not isinstance(target_workspace_id, str) or not target_workspace_id:
            raise ValueError("target_workspace_id is required")

        async with db_session() as session:
            snapshot = await session.get(WorkspaceSnapshotRecord, snapshot_id)
            if snapshot is None:
                raise ValueError(f"No such snapshot: {snapshot_id}")
            if snapshot.principal_id != ctx.principal_id:
                raise ValueError("snapshot belongs to a different principal")

            target = await session.get(ProvisionedResourceRecord, target_workspace_id)
            if target is None:
                raise ValueError(f"No such workspace: {target_workspace_id}")
            if target.principal_id != ctx.principal_id:
                raise ValueError("target workspace belongs to a different principal")
            if target.status != "active":
                raise ValueError(f"target workspace is {target.status}")

            target_path = Path(target.uri)
            restored_files = 0
            for file_entry in snapshot.files_json:
                rel_path = file_entry["path"]
                # SECURITY: never write outside the workspace (path traversal)
                target_file = (target_path / rel_path).resolve()
                if not str(target_file).startswith(str(target_path.resolve())):
                    raise ValueError(f"path traversal detected: {rel_path}")
                target_file.parent.mkdir(parents=True, exist_ok=True)
                # In a real implementation, we would copy from the snapshot's source workspace.
                # For now, we just recreate empty files (the snapshot is metadata-only here).
                # A future cycle will add byte-level restore from a content store.
                target_file.touch()
                restored_files += 1

            await session.commit()

        return {"restored_files": restored_files, "total_bytes": snapshot.total_bytes}

    async def workspace_promote_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from wax.state.engine import db_session
        from wax.state.provisioning_models import ProvisionedResourceRecord

        workspace_id = inputs.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id is required")

        async with db_session() as session:
            resource = await session.get(ProvisionedResourceRecord, workspace_id)
            if resource is None:
                raise ValueError(f"No such workspace: {workspace_id}")
            if resource.principal_id != ctx.principal_id:
                raise ValueError("workspace belongs to a different principal")
            if resource.status != "active":
                raise ValueError(f"workspace is {resource.status}")
            resource.expires_at = None  # permanent
            await session.commit()

        return {"promoted": True, "permanent_resource_id": workspace_id}

    async def artifact_capture_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        import hashlib
        from pathlib import Path

        from ulid import ULID

        from wax.state.artifact_models import ArtifactRecord
        from wax.state.engine import db_session
        from wax.state.provisioning_models import ProvisionedResourceRecord

        workspace_id = inputs.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise ValueError("workspace_id is required")
        path = inputs.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("path is required")
        filename = inputs.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ValueError("filename is required")
        if path.startswith("/"):
            raise ValueError("path must be workspace-relative")

        async with db_session() as session:
            resource = await session.get(ProvisionedResourceRecord, workspace_id)
            if resource is None:
                raise ValueError(f"No such workspace: {workspace_id}")
            if resource.principal_id != ctx.principal_id:
                raise ValueError("workspace belongs to a different principal")
            if resource.status != "active":
                raise ValueError(f"workspace is {resource.status}")

            file_path = Path(resource.uri) / path
            if not file_path.exists():
                raise ValueError(f"file does not exist: {path}")
            size = file_path.stat().st_size
            with open(file_path, "rb") as f:
                sha = hashlib.sha256(f.read()).hexdigest()

            artifact = ArtifactRecord(
                id=str(ULID()),
                principal_id=ctx.principal_id,
                workspace_resource_id=workspace_id,
                filename=filename,
                path=path,
                sha256=sha,
                size_bytes=size,
                source="capability:artifact.capture",
                execution_id=ctx.request_id or ctx.execution_id,
                metadata_json={},
            )
            session.add(artifact)
            await session.flush()
            await session.commit()

        return {
            "artifact_id": artifact.id,
            "sha256": sha,
            "size_bytes": size,
            "filename": filename,
        }

    async def artifact_list_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from sqlalchemy import select

        from wax.state.artifact_models import ArtifactRecord
        from wax.state.engine import db_session

        objective_id = inputs.get("objective_id")
        async with db_session() as session:
            stmt = (
                select(ArtifactRecord)
                .where(ArtifactRecord.principal_id == ctx.principal_id)
                .order_by(ArtifactRecord.created_at.desc())
            )
            if objective_id:
                # Filter by objective (artifacts don't have objective_id directly,
                # but they have execution_id; this is a stub for now)
                pass
            records = (await session.execute(stmt.limit(50))).scalars().all()
            await session.commit()

        return {
            "artifacts": [
                {
                    "artifact_id": a.id,
                    "filename": a.filename,
                    "sha256": a.sha256[:12],
                    "size_bytes": a.size_bytes,
                    "source": a.source,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in records
            ],
            "count": len(records),
        }

    async def artifact_retrieve_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        import hashlib
        from pathlib import Path

        from wax.state.artifact_models import ArtifactRecord
        from wax.state.engine import db_session
        from wax.state.provisioning_models import ProvisionedResourceRecord

        artifact_id = inputs.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("artifact_id is required")

        async with db_session() as session:
            artifact = await session.get(ArtifactRecord, artifact_id)
            if artifact is None:
                raise ValueError(f"No such artifact: {artifact_id}")
            if artifact.principal_id != ctx.principal_id:
                raise ValueError("artifact belongs to a different principal")

            # Re-verify integrity if the file still exists
            integrity_verified = False
            if artifact.workspace_resource_id:
                resource = await session.get(
                    ProvisionedResourceRecord, artifact.workspace_resource_id
                )
                if resource and resource.status == "active" and resource.uri:
                    file_path = Path(resource.uri) / artifact.path
                    if file_path.exists():
                        with open(file_path, "rb") as f:
                            actual_sha = hashlib.sha256(f.read()).hexdigest()
                        integrity_verified = actual_sha == artifact.sha256

            await session.commit()

        return {
            "artifact": {
                "artifact_id": artifact.id,
                "filename": artifact.filename,
                "sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
                "source": artifact.source,
                "workspace_resource_id": artifact.workspace_resource_id,
                "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
            },
            "integrity_verified": integrity_verified,
        }

    registry.register(WORKSPACE_SNAPSHOT_DESCRIPTOR, workspace_snapshot_impl)
    registry.register(WORKSPACE_RESTORE_DESCRIPTOR, workspace_restore_impl)
    registry.register(WORKSPACE_PROMOTE_DESCRIPTOR, workspace_promote_impl)
    registry.register(ARTIFACT_CAPTURE_DESCRIPTOR, artifact_capture_impl)
    registry.register(ARTIFACT_LIST_DESCRIPTOR, artifact_list_impl)
    registry.register(ARTIFACT_RETRIEVE_DESCRIPTOR, artifact_retrieve_impl)

    # ========================================================================
    # ADR-0043 (Phase 10): Media + Delivery completion
    # ========================================================================

    async def delivery_status_impl(
        inputs: dict[str, Any], ctx: InvocationContext
    ) -> dict[str, Any]:
        from sqlalchemy import select

        from wax.state.delivery_models import DeliveryRecord
        from wax.state.engine import db_session

        delivery_id = inputs.get("delivery_id")
        execution_id = inputs.get("execution_id")
        if not delivery_id and not execution_id:
            raise ValueError("either delivery_id or execution_id is required")

        async with db_session() as session:
            if delivery_id:
                records = (
                    (
                        await session.execute(
                            select(DeliveryRecord)
                            .where(DeliveryRecord.id == delivery_id)
                            .where(DeliveryRecord.principal_id == ctx.principal_id)
                        )
                    )
                    .scalars()
                    .all()
                )
            else:
                records = (
                    (
                        await session.execute(
                            select(DeliveryRecord)
                            .where(DeliveryRecord.execution_id == execution_id)
                            .where(DeliveryRecord.principal_id == ctx.principal_id)
                            .order_by(DeliveryRecord.created_at.desc())
                            .limit(20)
                        )
                    )
                    .scalars()
                    .all()
                )
            await session.commit()

        # Return metadata only — NEVER the message text
        return {
            "deliveries": [
                {
                    "delivery_id": r.id,
                    "interface": r.interface_kind,
                    "recipient_id": r.recipient_id,
                    "status": r.status,
                    "attempts": r.attempts,
                    "max_attempts": r.max_attempts,
                    "last_error": (r.last_error or "")[:200],
                    "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
                    "next_attempt_at": r.next_attempt_at.isoformat() if r.next_attempt_at else None,
                    "source": r.source,
                }
                for r in records
            ],
            "count": len(records),
        }

    async def delivery_retry_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from datetime import UTC, datetime

        from wax.state.delivery_models import DeliveryRecord
        from wax.state.engine import db_session

        delivery_id = inputs.get("delivery_id")
        if not isinstance(delivery_id, str) or not delivery_id:
            raise ValueError("delivery_id is required")

        async with db_session() as session:
            record = await session.get(DeliveryRecord, delivery_id)
            if record is None:
                raise ValueError(f"No such delivery: {delivery_id}")
            if record.principal_id != ctx.principal_id:
                raise ValueError("delivery belongs to a different principal")
            if record.status == "delivered":
                return {"retried": False, "status": "delivered", "reason": "already delivered"}
            if record.attempts >= record.max_attempts:
                return {"retried": False, "status": "failed", "reason": "max_attempts exhausted"}
            # Reset to pending for the maintenance loop
            record.status = "pending"
            record.next_attempt_at = datetime.now(UTC)
            await session.commit()

        return {
            "retried": True,
            "status": "pending",
            "next_attempt_at": record.next_attempt_at.isoformat(),
        }

    registry.register(DELIVERY_STATUS_DESCRIPTOR, delivery_status_impl)
    registry.register(DELIVERY_RETRY_DESCRIPTOR, delivery_retry_impl)
    log.info("capability.runtime_registered", count=37)


# --- memory.* (memory as a mechanism, not an AI chore) --------------------
#
# The runtime owns memory storage; the AI owns interpretation. Before
# these capabilities existed, the AI could not deliberately persist a
# fact ("user is preparing for WAEC physics"), search its own evidence,
# or honor a withdrawal request ("forget that") — only implicit episodic
# writes happened. Memory operations now cross the SAME gate chain as
# every other effect: agency → budget → authority → invoker → audit.

MEMORY_KINDS = ("episodic", "semantic", "procedural", "contextual", "external")

# Typed evidence relationships (ADR-0022). Supersession is NOT here: it
# is lifecycle (supersedes=), not a knowledge edge.
MEMORY_LINK_KINDS = (
    "supports",
    "contradicts",
    "derived_from",
    "related_to",
    "depends_on",  # ADR-0036 Phase 3: dependency edge — A needs B to be true
    "conflicts_with",  # ADR-0036 Phase 3: explicit conflict graph edge
)

MEMORY_STORE_DESCRIPTOR = CapabilityDescriptor(
    name="memory.store",
    description="Persist a memory for the current principal (structured "
    "evidence, not a frozen category). Pass expires_at for anything that "
    "should be forgotten automatically. Pass supersedes=<memory_id> when "
    "this record REPLACES an older active memory of the same principal "
    "(revision: the old record stays for audit but leaves retrieval). "
    "Optional importance (0-1) weights retrieval; observed_at records "
    "when the fact was observed (vs written); links=[{memory_id, kind}] "
    "adds typed edges (supports/contradicts/derived_from/related_to/"
    "depends_on/conflicts_with) to existing memories. Optional "
    "objective_id links this memory to the objective it supports/"
    "evidences (ADR-0036).",
    version="1.3.0",
    input_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": list(MEMORY_KINDS),
                "default": "semantic",
            },
            "content": {
                "type": "object",
                "description": "Structured evidence (JSON object)",
            },
            "summary": {"type": "string", "maxLength": 2000},
            "expires_at": {
                "type": "string",
                "description": "ISO-8601 datetime; after this the runtime forgets it",
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "importance": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "How much this matters for retrieval (default neutral)",
            },
            "observed_at": {
                "type": "string",
                "description": "ISO-8601: when the fact was observed (may differ from now)",
            },
            "objective_id": {
                "type": "string",
                "description": (
                    "Optional objective this memory supports/evidences "
                    "(ADR-0036 Phase 3). The objective must belong to "
                    "the calling principal."
                ),
            },
            "links": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": list(MEMORY_LINK_KINDS),
                        },
                    },
                    "required": ["memory_id", "kind"],
                },
                "description": "Typed edges from this record to existing memories",
            },
            "supersedes": {
                "type": "string",
                "description": "memory_id this record replaces (ownership-checked)",
            },
        },
        "required": ["content"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string"},
            "kind": {"type": "string"},
            "superseded": {"type": ["string", "null"]},
            "linked": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "kind": {"type": "string"},
                    },
                },
            },
        },
    },
    required_permission="memory.write",
    timeout_seconds=5.0,
    idempotent=False,
    is_destructive=False,
)

MEMORY_SEARCH_DESCRIPTOR = CapabilityDescriptor(
    name="memory.search",
    description="Search the principal's active memories by relevance to a "
    "query. Returns evidence (id, kind, summary, score) — never authority.",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        },
        "required": ["query"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "memories": {"type": "array"},
        },
    },
    required_permission="memory.read",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

MEMORY_FORGET_DESCRIPTOR = CapabilityDescriptor(
    name="memory.forget",
    description="Forget one of the principal's own memories by id (soft "
    "delete: record retained for audit, excluded from retrieval).",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string"},
            "forgotten": {"type": "boolean"},
            "already_forgotten": {"type": "boolean"},
        },
    },
    required_permission="memory.write",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=True,  # crosses the destructive agency gate
)

# Consolidation bounds: at most this many sources per consolidated record
# (keeps the operation bounded and the provenance chain inspectable).
MAX_CONSOLIDATION_SOURCES = 20

MEMORY_CONSOLIDATE_DESCRIPTOR = CapabilityDescriptor(
    name="memory.consolidate",
    description=(
        "Consolidate several of the principal's memories into ONE durable "
        "representation (e.g. transient session evidence into a stable "
        "fact). The runtime verifies every source, links provenance, and "
        "by default supersedes the sources (they stay queryable for audit "
        "but leave retrieval). You interpret the meaning; the runtime "
        "enforces the lifecycle."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "source_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": MAX_CONSOLIDATION_SOURCES,
                "description": "Active memories of this principal to consolidate",
            },
            "content": {
                "type": "object",
                "description": "The consolidated evidence (JSON object)",
            },
            "summary": {"type": "string", "maxLength": 2000},
            "kind": {
                "type": "string",
                "enum": list(MEMORY_KINDS),
                "default": "semantic",
                "description": "Durable representation kind",
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "expires_at": {
                "type": "string",
                "description": "Optional retention for the consolidated record",
            },
            "supersede_sources": {
                "type": "boolean",
                "default": True,
                "description": "Mark sources superseded by the new record",
            },
        },
        "required": ["source_ids", "content"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "memory_id": {"type": "string"},
            "consolidated_count": {"type": "integer"},
            "superseded_ids": {"type": "array"},
        },
    },
    required_permission="memory.write",
    timeout_seconds=5.0,
    idempotent=False,
    is_destructive=False,
)


async def memory_store_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    from datetime import datetime

    from wax.memory.contracts import MemoryCreate, MemoryKind
    from wax.memory.repository import MemoryRepository
    from wax.state.engine import db_session

    content = inputs.get("content")
    if not isinstance(content, dict) or not content:
        raise ValueError("content must be a non-empty JSON object")

    kind = inputs.get("kind", "semantic")
    if kind not in MEMORY_KINDS:
        raise ValueError(f"kind must be one of {list(MEMORY_KINDS)}")

    summary = inputs.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("summary must be a string")

    expires_at = None
    if inputs.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(str(inputs["expires_at"]).replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"expires_at is not a valid ISO-8601 datetime: {e}") from e

    confidence = inputs.get("confidence")
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        raise ValueError("confidence must be between 0 and 1")

    importance = inputs.get("importance")
    if importance is not None and not (0.0 <= float(importance) <= 1.0):
        raise ValueError("importance must be between 0 and 1")

    observed_at = None
    if inputs.get("observed_at"):
        try:
            observed_at = datetime.fromisoformat(str(inputs["observed_at"]).replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"observed_at is not a valid ISO-8601 datetime: {e}") from e

    links = inputs.get("links")
    if links is not None:
        if not isinstance(links, list) or len(links) > 10:
            raise ValueError("links must be a list of at most 10 {memory_id, kind}")
        for entry in links:
            if (
                not isinstance(entry, dict)
                or not entry.get("memory_id")
                or entry.get("kind") not in MEMORY_LINK_KINDS
            ):
                raise ValueError(
                    "each link must be {memory_id, kind} with kind one of "
                    f"{list(MEMORY_LINK_KINDS)}"
                )

    supersedes_id = inputs.get("supersedes")
    if supersedes_id is not None and not (isinstance(supersedes_id, str) and supersedes_id):
        raise ValueError("supersedes must be a memory_id string")

    async with db_session() as session:
        repo = MemoryRepository(session)

        # Verify BEFORE creating the replacement so a bad request creates
        # nothing (no orphan evidence) — same discipline for supersedes
        # and links: a refused link target is a LOUD error, never a
        # silently skipped edge (the model must know its link did not land).
        if supersedes_id:
            old = await repo.get(supersedes_id)
            if old is None or old.status != "active":
                raise ValueError(f"No active memory {supersedes_id} to supersede")
            if old.principal_id != ctx.principal_id:
                raise ValueError("supersedes targets another principal's memory")
        for entry in links or []:
            target = await repo.get(str(entry["memory_id"]))
            if target is None or target.status != "active":
                raise ValueError(f"No active memory {entry['memory_id']} to link to")
            if target.principal_id != ctx.principal_id:
                raise ValueError(f"links target another principal's memory ({entry['memory_id']})")

        record = await repo.create(
            MemoryCreate(
                principal_id=ctx.principal_id,
                kind=MemoryKind(kind),
                content=content,
                provenance="model_observation",
                source_execution_id=ctx.execution_id,
                confidence=float(confidence) if confidence is not None else None,
                importance=float(importance) if importance is not None else None,
                observed_at=observed_at,
                expires_at=expires_at,
                summary=summary[:2000] if summary else None,
            )
        )

        # ADR-0036 (Phase 3): optional objective linkage. The intelligence
        # may declare that this memory supports/evidences a specific
        # objective. The objective_id is validated for ownership before
        # being attached.
        objective_id_raw = inputs.get("objective_id")
        if objective_id_raw:
            if not isinstance(objective_id_raw, str) or not objective_id_raw:
                raise ValueError("objective_id must be a non-empty string")
            from wax.objective.repository import ObjectiveRepository

            obj_repo = ObjectiveRepository(session)
            objective = await obj_repo.get(objective_id_raw)
            if objective is None:
                raise ValueError(f"No such objective: {objective_id_raw}")
            if objective.principal_id != ctx.principal_id:
                raise ValueError("objective_id belongs to a different principal")
            record.objective_id = objective_id_raw
        if supersedes_id:
            superseded_rows = await repo.supersede(supersedes_id, record.id)
            if not superseded_rows:
                raise ValueError(
                    f"memory {supersedes_id} could not be superseded (it is "
                    "not an active memory of yours) — the new record was "
                    "created and linked to nothing it did not replace"
                )
        linked: list[dict] = []
        for entry in links or []:
            edge = await repo.link(
                record.id,
                str(entry["memory_id"]),
                str(entry["kind"]),
                execution_id=ctx.execution_id,
            )
            if edge is not None:
                linked.append({"memory_id": edge.to_memory_id, "kind": edge.kind})
        await session.commit()

    return {
        "memory_id": record.id,
        "kind": record.kind,
        "superseded": supersedes_id,
        "linked": linked,
    }


async def memory_search_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    from wax.memory.repository import MemoryRepository
    from wax.state.engine import db_session

    query = inputs.get("query")
    if not query or not isinstance(query, str):
        raise ValueError("query is required")
    limit = int(inputs.get("limit", 5) or 5)

    async with db_session() as session:
        results = await MemoryRepository(session).search_relevant(
            ctx.principal_id, query, limit=limit
        )

    return {
        "count": len(results),
        "memories": [
            {
                "id": record.id,
                "kind": record.kind,
                "summary": record.summary or str(record.content)[:200],
                "created_at": record.created_at.isoformat() if record.created_at else None,
                "score": round(score, 4),
            }
            for record, score in results
        ],
    }


async def memory_forget_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    from wax.memory.repository import MemoryRepository
    from wax.state.engine import db_session

    memory_id = inputs.get("memory_id")
    if not memory_id or not isinstance(memory_id, str):
        raise ValueError("memory_id is required")

    async with db_session() as session:
        repo = MemoryRepository(session)
        memory = await repo.get(memory_id)
        if memory is None:
            raise ValueError(f"No memory {memory_id}")
        if memory.principal_id != ctx.principal_id:
            # Ownership boundary: a principal cannot forget another's memory.
            raise ValueError("memory_id belongs to a different principal")
        if memory.status == "forgotten":
            # The descriptor declares this capability idempotent — make the
            # implementation honor its own contract: forgetting an already
            # forgotten memory is an honest no-op, not a failure.
            await session.commit()
            return {"memory_id": memory_id, "forgotten": True, "already_forgotten": True}
        if memory.status != "active":
            raise ValueError(f"memory {memory_id} is {memory.status} and cannot be forgotten")
        forgotten = await repo.forget(memory_id)
        await session.commit()

    return {"memory_id": memory_id, "forgotten": forgotten}


async def memory_consolidate_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    """Consolidate transient evidence into one durable representation.

    The intelligence performs the interpretation (it writes the new
    content/summary); the runtime enforces the lifecycle mechanics: every
    source must be an ACTIVE memory of the calling principal, provenance
    records the operation, and sources are (by default) superseded by the
    new record — retained for audit, excluded from retrieval. This is the
    WAX shape of memory consolidation: evidence → evaluation (the model's)
    → durable representation → supersession — with NO hardcoded rule
    engine deciding WHAT the durable meaning is.
    """
    from datetime import datetime

    from wax.memory.contracts import MemoryCreate, MemoryKind
    from wax.memory.repository import MemoryRepository
    from wax.state.engine import db_session

    source_ids = inputs.get("source_ids")
    if (
        not isinstance(source_ids, list)
        or not source_ids
        or len(source_ids) > MAX_CONSOLIDATION_SOURCES
        or not all(isinstance(s, str) and s for s in source_ids)
    ):
        raise ValueError(
            f"source_ids must be a non-empty list of memory ids (max {MAX_CONSOLIDATION_SOURCES})"
        )
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("source_ids must be distinct")

    content = inputs.get("content")
    if not isinstance(content, dict) or not content:
        raise ValueError("content must be a non-empty JSON object")

    kind = inputs.get("kind", "semantic")
    if kind not in MEMORY_KINDS:
        raise ValueError(f"kind must be one of {list(MEMORY_KINDS)}")

    summary = inputs.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise ValueError("summary must be a string")

    confidence = inputs.get("confidence")
    if confidence is not None and not (0.0 <= float(confidence) <= 1.0):
        raise ValueError("confidence must be between 0 and 1")

    expires_at = None
    if inputs.get("expires_at"):
        try:
            expires_at = datetime.fromisoformat(str(inputs["expires_at"]).replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"expires_at is not a valid ISO-8601 datetime: {e}") from e

    supersede_sources = inputs.get("supersede_sources", True)
    if not isinstance(supersede_sources, bool):
        raise ValueError("supersede_sources must be a boolean")

    async with db_session() as session:
        repo = MemoryRepository(session)

        # Verify EVERY source before creating anything: a consolidation
        # that would partially fail must not leave partial state.
        sources = []
        for sid in source_ids:
            memory = await repo.get(sid)
            if memory is None or memory.status != "active":
                raise ValueError(f"Source {sid} is not an active memory")
            if memory.principal_id != ctx.principal_id:
                raise ValueError(f"Source {sid} belongs to a different principal")
            sources.append(memory)

        # Provenance: when sources stay active (no supersession), the link
        # to them lives in the content itself; when superseded, the
        # superseded_by chain is the audit trail.
        stored_content = dict(content)
        if not supersede_sources:
            stored_content["consolidated_from"] = source_ids

        record = await repo.create(
            MemoryCreate(
                principal_id=ctx.principal_id,
                kind=MemoryKind(kind),
                content=stored_content,
                provenance="consolidation",
                source_execution_id=ctx.execution_id,
                confidence=float(confidence) if confidence is not None else None,
                expires_at=expires_at,
                summary=summary[:2000] if summary else None,
            )
        )

        # ADR-0036 (Phase 3): record the consolidation provenance as a
        # forward chain on the new record. The backward chain
        # (source.superseded_by = record.id) is set below when
        # supersede_sources is True; the forward chain
        # (record.consolidation_sources = [source1, ...]) is the
        # inspectable "where I came from" answer.
        record.consolidation_sources = list(source_ids)

        # Structured provenance edges (ADR-0022): the consolidation is
        # derived_from every source — queryable, not just JSON-in-content.
        # Created BEFORE supersession: sources must still be ACTIVE for
        # edges to be valid.
        for sid in source_ids:
            await repo.link(record.id, sid, "derived_from", execution_id=ctx.execution_id)
        superseded_ids: list[str] = []
        supersede_refused_ids: list[str] = []
        if supersede_sources:
            for sid in source_ids:
                if await repo.supersede(sid, record.id):
                    superseded_ids.append(sid)
                else:
                    # A verified source failed to supersede (a racing
                    # writer changed its state between verify and
                    # supersede). Swallowing this would make the
                    # reported outcome a partial lie — report it.
                    supersede_refused_ids.append(sid)
        await session.commit()

    return {
        "memory_id": record.id,
        "consolidated_count": len(source_ids),
        "superseded_ids": superseded_ids,
        "supersede_refused_ids": supersede_refused_ids,
    }


# --- memory.link (typed evidence relationships, ADR-0022) -----------------
#
# The intelligence may propose how memories relate; the runtime retains
# authority: both endpoints must be the principal's ACTIVE memories, the
# kind is validated against the fixed vocabulary, self-links and
# cross-principal edges are refused, and every edge carries provenance.
# Supersession deliberately is NOT a link kind — it is lifecycle.

MEMORY_LINK_DESCRIPTOR = CapabilityDescriptor(
    name="memory.link",
    description="Record a typed relationship between two of this "
    "principal's memories: supports, contradicts, derived_from, or "
    "related_to. Retrieval uses links to pull in related evidence. "
    "Idempotent: re-linking the same pair returns the existing edge.",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "from_memory_id": {"type": "string"},
            "to_memory_id": {"type": "string"},
            "kind": {"type": "string", "enum": list(MEMORY_LINK_KINDS)},
        },
        "required": ["from_memory_id", "to_memory_id", "kind"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "link_id": {"type": "string"},
            "from_memory_id": {"type": "string"},
            "to_memory_id": {"type": "string"},
            "kind": {"type": "string"},
            "existed": {"type": "boolean"},
        },
    },
    required_permission="memory.write",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)


async def memory_link_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    from wax.memory.repository import MemoryRepository
    from wax.state.engine import db_session

    from_id = inputs.get("from_memory_id")
    to_id = inputs.get("to_memory_id")
    kind = inputs.get("kind")
    if not from_id or not isinstance(from_id, str):
        raise ValueError("from_memory_id is required")
    if not to_id or not isinstance(to_id, str):
        raise ValueError("to_memory_id is required")
    if kind not in MEMORY_LINK_KINDS:
        raise ValueError(f"kind must be one of {list(MEMORY_LINK_KINDS)}")

    async with db_session() as session:
        repo = MemoryRepository(session)
        # Ownership is enforced HERE (both endpoints), not just in the
        # repository: a cross-principal edge attempt is a loud denial.
        for mid in (from_id, to_id):
            memory = await repo.get(mid)
            if memory is None:
                raise ValueError(f"No such memory: {mid}")
            if memory.principal_id != ctx.principal_id:
                raise ValueError(f"memory {mid} belongs to a different principal")
        preexisting = [
            edge
            for edge, _ in await repo.links_for(from_id, kind=kind)
            if edge.to_memory_id == to_id
        ]
        edge = await repo.link(from_id, to_id, kind, execution_id=ctx.execution_id)
        if edge is None:
            raise ValueError(
                "link refused: both memories must be ACTIVE and belong to "
                "the same principal (and kind must be valid)"
            )
        await session.commit()

    return {
        "link_id": edge.id,
        "from_memory_id": edge.from_memory_id,
        "to_memory_id": edge.to_memory_id,
        "kind": edge.kind,
        "existed": bool(preexisting),
    }


# --- objective.* (the intelligence can drive its principal's objectives) ---
#
# ADR-0020. Before these capabilities existed the objective record was a
# runtime-internal artifact: the bridge created one per interaction, and
# the intelligence could neither see its principal's broader objectives
# nor continue one across messages. That made every long-running human
# goal a chain of sibling single-message objectives — continuity by
# accident, not by representation. These capabilities close that with
# generic mechanisms ONLY: list (visibility), resume (continue a prior
# objective with this interaction's execution), update_status (close
# with recorded evidence). No domain kinds, no task taxonomy — the
# description stays free text the intelligence interprets.

OBJECTIVE_LIST_DESCRIPTOR = CapabilityDescriptor(
    name="objective.list",
    description=(
        "List the current principal's objectives (what the human wants "
        "to accomplish), most recent first, with status and execution "
        "counts. Use this to discover ongoing work before assuming a "
        "request is new."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Optional filter: pending, in_progress, waiting, "
                "awaiting_human, succeeded, failed, cancelled, abandoned",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
    },
    required_permission="objective.read",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

OBJECTIVE_RESUME_DESCRIPTOR = CapabilityDescriptor(
    name="objective.resume",
    description=(
        "Continue an existing objective of this principal with the "
        "current interaction: the execution now advances THAT objective "
        "instead of a fresh per-message one, and the interaction history "
        "of both records the link. Use when the human's message clearly "
        "belongs to earlier work (for example 'continue that')."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "objective_id": {"type": "string"},
            "note": {
                "type": "string",
                "maxLength": 2000,
                "description": "Why this message continues the objective",
            },
        },
        "required": ["objective_id"],
    },
    required_permission="objective.write",
    timeout_seconds=5.0,
    idempotent=False,
    is_destructive=False,
)

OBJECTIVE_UPDATE_STATUS_DESCRIPTOR = CapabilityDescriptor(
    name="objective.update_status",
    description=(
        "Close or cancel one of this principal's objectives with the "
        "evidence that justifies it. Allowed targets: succeeded, failed, "
        "cancelled, abandoned. Completion must carry real evidence from "
        "this interaction (capability results, artifacts) — the runtime "
        "records the claim with its evidence; it does not verify intent."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "objective_id": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["succeeded", "failed", "cancelled", "abandoned"],
            },
            "evidence": {
                "type": "string",
                "maxLength": 2000,
                "description": "What actually happened that justifies this status",
            },
        },
        "required": ["objective_id", "status", "evidence"],
    },
    required_permission="objective.write",
    timeout_seconds=5.0,
    idempotent=False,
    is_destructive=False,
)

_VALID_OBJECTIVE_CLOSE_STATUSES = ("succeeded", "failed", "cancelled", "abandoned")


async def objective_list_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    from wax.objective.contracts import ObjectiveStatus
    from wax.objective.repository import ObjectiveRepository
    from wax.state.engine import db_session

    status = inputs.get("status")
    try:
        limit = int(inputs.get("limit", 20))
    except (TypeError, ValueError) as e:
        raise ValueError("limit must be an integer") from e
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")

    async with db_session() as session:
        repo = ObjectiveRepository(session)
        if status is not None:
            valid = {s.value for s in ObjectiveStatus}
            if status not in valid:
                raise ValueError(f"status must be one of {sorted(valid)}")
        records = await repo.list_for_principal(ctx.principal_id, status=status, limit=limit)
        items = []
        for r in records:
            history = await repo.list_executions(r.id, limit=200)
            items.append(
                {
                    "objective_id": r.id,
                    "description": r.description[:500],
                    "kind": r.kind,
                    "status": r.status,
                    "success_criteria": r.success_criteria,
                    "created_at": r.created_at.isoformat(),
                    "updated_at": r.updated_at.isoformat(),
                    "execution_count": len(history),
                    "last_execution_outcome": (
                        history[-1].outcome if history and history[-1].outcome else None
                    ),
                }
            )
        await session.commit()

    return {"count": len(items), "objectives": items}


async def objective_resume_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
    from wax.objective.contracts import ACTIVE_ELIGIBLE_STATES, ObjectiveStatus
    from wax.objective.evidence import objective_for_execution
    from wax.objective.repository import ObjectiveRepository
    from wax.state.engine import db_session

    objective_id = inputs.get("objective_id")
    if not objective_id or not isinstance(objective_id, str):
        raise ValueError("objective_id is required")
    note = inputs.get("note")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string")

    # On the live bridge path request_id IS the bridge execution; the
    # invoker-internal execution id is the fallback (direct calls).
    execution_id = ctx.request_id or ctx.execution_id

    async with db_session() as session:
        repo = ObjectiveRepository(session)
        target = await repo.get(objective_id)
        if target is None or target.principal_id != ctx.principal_id:
            # Do not leak other principals' objectives.
            raise ValueError(f"No such objective for this principal: {objective_id}")
        if target.status not in ACTIVE_ELIGIBLE_STATES:
            raise ValueError(f"Objective {objective_id} is {target.status} and cannot be resumed")

        previous_objective_id: str | None = None
        current = await objective_for_execution(session, execution_id)
        if current is not None and current.id != target.id:
            previous_objective_id = current.id
            if current.status in ("pending", "in_progress", "waiting", "awaiting_human"):
                # The per-message objective is superseded by the resumed
                # one: close its history honestly and cancel it.
                await repo.record_execution_end(
                    current.id, execution_id or "", outcome="superseded"
                )
                await repo.transition(current.id, ObjectiveStatus.CANCELLED)

        # Redirect the execution to the resumed objective: new history
        # row + current pointer + active transition.
        await repo.record_execution_start(
            target.id, execution_id or ctx.execution_id, kind="bridge"
        )
        await repo.transition(target.id, ObjectiveStatus.IN_PROGRESS)

        context = dict(target.context or {})
        resumes = list(context.get("resumes") or [])
        resumes.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "execution_id": execution_id,
                "note": (note or "")[:2000] or None,
                "from_objective_id": previous_objective_id,
            }
        )
        context["resumes"] = resumes[-20:]
        target.context = context
        await session.commit()

    return {
        "objective_id": objective_id,
        "status": "in_progress",
        "superseded_objective_id": previous_objective_id,
    }


async def objective_update_status_impl(
    inputs: dict[str, Any], ctx: InvocationContext
) -> dict[str, Any]:
    from wax.objective.repository import ObjectiveRepository
    from wax.state.engine import db_session

    objective_id = inputs.get("objective_id")
    if not objective_id or not isinstance(objective_id, str):
        raise ValueError("objective_id is required")
    status = inputs.get("status")
    if status not in _VALID_OBJECTIVE_CLOSE_STATUSES:
        raise ValueError(f"status must be one of {list(_VALID_OBJECTIVE_CLOSE_STATUSES)}")
    evidence = inputs.get("evidence")
    if not evidence or not isinstance(evidence, str):
        raise ValueError("evidence is required: the runtime records WHY with the status")

    async with db_session() as session:
        repo = ObjectiveRepository(session)
        record = await repo.get(objective_id)
        if record is None or record.principal_id != ctx.principal_id:
            raise ValueError(f"No such objective for this principal: {objective_id}")
        if record.status in ("succeeded", "failed", "cancelled", "abandoned"):
            raise ValueError(f"Objective {objective_id} is already terminal ({record.status})")

        # CV-15 guard: `succeeded` is a terminal, immutable claim. The
        # bridge path already refuses to fabricate it while durable work
        # is outstanding (objective_has_outstanding_work); this model-
        # facing close path enforces the SAME runtime evidence rule.
        # Terminal states cannot be revised, so a fabricated `succeeded`
        # would be a permanent lie.
        if status == "succeeded":
            from wax.objective.evidence import objective_has_outstanding_work

            if await objective_has_outstanding_work(session, objective_id):
                raise ValueError(
                    f"Objective {objective_id} still has outstanding durable "
                    "work; `succeeded` requires zero outstanding work "
                    "(the runtime, not the model, owns terminal evidence)"
                )

        # Record the claim WITH its evidence (mission §24: the runtime
        # retains the evidence supporting the completed state), then
        # transition. Terminal statuses accept no further transitions.
        context = dict(record.context or {})
        close_events = list(context.get("status_evidence") or [])
        close_events.append(
            {
                "at": datetime.now(UTC).isoformat(),
                "execution_id": ctx.request_id or ctx.execution_id,
                "status": status,
                "evidence": evidence[:2000],
            }
        )
        context["status_evidence"] = close_events[-20:]
        record.context = context

        closed = await repo.record_execution_end(
            objective_id, ctx.request_id or ctx.execution_id, outcome=status
        )
        ok = await repo.transition(objective_id, status)
        if not ok:
            raise ValueError(
                f"Cannot transition objective {objective_id} from {record.status} to {status}"
            )
        await session.commit()

    return {
        "objective_id": objective_id,
        "status": status,
        "history_rows_closed": closed,
    }


# ============================================================================
# ADR-0038 (Phase 5): Environment Negotiation — module-level descriptor
# ============================================================================
# The impl is defined inside register_runtime_capabilities so it can close
# over . The descriptor is at module level so it can be imported
# by tests.

ENVIRONMENT_REQUEST_DESCRIPTOR = CapabilityDescriptor(
    name="environment.request",
    description=(
        "Declare an environment requirement and have the runtime plan + "
        "provision a bounded environment (workspace, isolation, network "
        "policy, tools, credential handles). The intelligence receives "
        "opaque handles; it NEVER sees host paths or raw secrets."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "purpose": {"type": "string", "maxLength": 200},
            "workspace": {
                "type": "object",
                "properties": {
                    "persistent": {"type": "boolean"},
                    "disk_bytes": {"type": "integer", "minimum": 0},
                    "description": {"type": "string", "maxLength": 200},
                },
            },
            "execution": {
                "type": "object",
                "properties": {
                    "cpu_seconds": {"type": "integer", "minimum": 0},
                    "memory_bytes": {"type": "integer", "minimum": 0},
                    "processes": {"type": "integer", "minimum": 0},
                    "timeout_seconds": {"type": "integer", "minimum": 0},
                },
            },
            "tools": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "acquire_if_missing": {"type": "boolean"},
                    },
                    "required": ["name"],
                },
            },
            "credentials": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "connector": {"type": "string"},
                        "scopes": {"type": "array", "items": {"type": "string"}},
                        "purpose": {"type": "string"},
                    },
                    "required": ["connector"],
                },
            },
            "network": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["none", "allowlisted", "open"]},
                    "allowlist": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                },
            },
            "isolation": {
                "type": "string",
                "enum": ["none", "namespace", "container"],
                "default": "none",
            },
            "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 86400},
        },
        "required": ["purpose"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "environment_id": {"type": "string"},
            "expires_at": {"type": "string"},
            "state": {"type": "string"},
            "degraded": {"type": "array", "items": {"type": "string"}},
            "workspace_id": {"type": ["string", "null"]},
            "credential_handles": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": ["string", "null"]},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=30.0,
    idempotent=False,
    is_destructive=False,
)


# ============================================================================
# ADR-0039 (Phase 6): Terminal Runtime — module-level descriptors
# ============================================================================

TERMINAL_SESSION_OPEN_DESCRIPTOR = CapabilityDescriptor(
    name="terminal.session.open",
    description=(
        "Open a persistent terminal session bound to an environment lease. "
        "The session's working_dir is workspace-relative; env_vars are "
        "validated (no secret-like names). The session has its own TTL "
        "(≤ the environment's TTL)."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "environment_id": {"type": "string"},
            "working_dir": {"type": "string", "default": "."},
            "env_vars": {"type": "object", "additionalProperties": {"type": "string"}},
            "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 3600},
        },
        "required": ["environment_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "state": {"type": "string"},
            "expires_at": {"type": "string"},
        },
    },
    required_permission="capability.invoke:terminal",
    timeout_seconds=10.0,
    idempotent=False,
    is_destructive=False,
)

TERMINAL_EXECUTE_DESCRIPTOR = CapabilityDescriptor(
    name="terminal.execute",
    description=(
        "Run a shell command in a terminal session. The command runs in "
        "the session's environment (workspace + isolation + network "
        "policy). stdout/stderr are bounded (default 64KB each). The "
        "command is killed on timeout (SIGKILL the entire process group)."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "command": {"type": "string", "maxLength": 8192},
            "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 600, "default": 30},
            "max_output_bytes": {
                "type": "integer",
                "minimum": 1024,
                "maximum": 1048576,
                "default": 65536,
            },
        },
        "required": ["session_id", "command"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "exit_code": {"type": "integer"},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "timed_out": {"type": "boolean"},
            "truncated": {"type": "boolean"},
            "duration_ms": {"type": "number"},
        },
    },
    required_permission="capability.invoke:terminal",
    timeout_seconds=600.0,
    idempotent=False,
    is_destructive=True,
)

TERMINAL_SESSION_CLOSE_DESCRIPTOR = CapabilityDescriptor(
    name="terminal.session.close",
    description="Close a terminal session and kill its process group.",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"session_id": {"type": "string"}},
        "required": ["session_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "closed": {"type": "boolean"},
            "state": {"type": "string"},
        },
    },
    required_permission="capability.invoke:terminal",
    timeout_seconds=10.0,
    idempotent=True,
    is_destructive=True,
)


# ============================================================================
# ADR-0040 (Phase 7): Credential Vault — module-level descriptors
# ============================================================================

CREDENTIAL_CONNECT_DESCRIPTOR = CapabilityDescriptor(
    name="credential.connect",
    description=(
        "Register a credential for a connector (resource type, NOT a "
        "brand: git_host, package_registry, cloud_deployment, file_storage, "
        "messaging). The secret is encrypted at rest; the runtime NEVER "
        "exposes it to the model. Returns an opaque connection_id."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "connector": {
                "type": "string",
                "description": "Resource type: git_host, package_registry, cloud_deployment, file_storage, messaging",
            },
            "secret": {"type": "string", "maxLength": 8192},
            "scopes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["connector", "secret"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "connection_id": {"type": "string"},
            "connector": {"type": "string"},
            "scopes": {"type": "array", "items": {"type": "string"}},
        },
    },
    required_permission="capability.invoke:credential",
    timeout_seconds=10.0,
    idempotent=False,
    is_destructive=True,
)

CREDENTIAL_REQUEST_DESCRIPTOR = CapabilityDescriptor(
    name="credential.request",
    description=(
        "Request a scoped grant for an objective/execution. The runtime "
        "validates the connection exists + has the requested scopes, "
        "creates a grant with TTL, returns an opaque handle. The vault "
        "injects the actual secret into the environment boundary at "
        "provisioning time — NEVER into the model's prompt."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "connection_id": {"type": "string"},
            "scopes": {"type": "array", "items": {"type": "string"}},
            "purpose": {"type": "string", "maxLength": 500},
            "objective_id": {"type": "string"},
            "ttl_seconds": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 3600},
        },
        "required": ["connection_id", "scopes"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "grant_id": {"type": "string"},
            "handle": {"type": "string"},
            "expires_at": {"type": "string"},
            "scopes": {"type": "array", "items": {"type": "string"}},
            "connector": {"type": "string"},
        },
    },
    required_permission="capability.invoke:credential",
    timeout_seconds=10.0,
    idempotent=False,
    is_destructive=True,
)

CREDENTIAL_LIST_DESCRIPTOR = CapabilityDescriptor(
    name="credential.list",
    description="List the principal's connections. Metadata only — NO secrets.",
    version="1.0.0",
    input_schema={"type": "object", "properties": {}},
    output_schema={
        "type": "object",
        "properties": {
            "connections": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer"},
        },
    },
    required_permission="capability.invoke:credential",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=True,
)

CREDENTIAL_REVOKE_DESCRIPTOR = CapabilityDescriptor(
    name="credential.revoke",
    description=(
        "Revoke a connection OR a grant. Immediate. The secret is purged "
        "from any in-flight injection."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "connection_id": {"type": "string"},
            "grant_id": {"type": "string"},
            "reason": {"type": "string", "maxLength": 500},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "revoked": {"type": "array", "items": {"type": "string"}},
        },
    },
    required_permission="capability.invoke:credential",
    timeout_seconds=10.0,
    idempotent=True,
    is_destructive=True,
)


# ============================================================================
# ADR-0041 (Phase 8): Generic Connector Runtime — module-level descriptors
# ============================================================================

CONNECTOR_DISCOVER_DESCRIPTOR = CapabilityDescriptor(
    name="connector.discover",
    description=(
        "Discover available connector definitions (resource types: "
        "git_host, package_registry, cloud_deployment, file_storage, "
        "messaging). The runtime understands resource types, NOT brands. "
        "The intelligence learns actual services (GitHub, GitLab, npm, "
        "PyPI, Railway, Fly.io, Google Drive, Dropbox) through the "
        "environment — no architectural change when a new platform appears."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "connector": {
                "type": "string",
                "description": "Optional filter: return only this connector type",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "connectors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "supported_scopes": {"type": "array", "items": {"type": "string"}},
                        "auth_methods": {"type": "array", "items": {"type": "string"}},
                        "version": {"type": "string"},
                    },
                },
            },
            "count": {"type": "integer"},
        },
    },
    required_permission="capability.invoke:connector",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

CONNECTOR_RESOLVE_DESCRIPTOR = CapabilityDescriptor(
    name="connector.resolve",
    description=(
        "Resolve a grant handle (from credential.request) to a service "
        "binding. Returns an opaque binding_handle + the discovered "
        "service_kind (e.g. github, gitlab — learned through the "
        "environment, NOT hardcoded) + available_operations for the "
        "granted scopes. The intelligence never sees raw secrets."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "grant_handle": {"type": "string"},
        },
        "required": ["grant_handle"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "binding_handle": {"type": "string"},
            "service_kind": {"type": "string"},
            "connector": {"type": "string"},
            "scopes": {"type": "array", "items": {"type": "string"}},
            "available_operations": {"type": "array", "items": {"type": "string"}},
        },
    },
    required_permission="capability.invoke:connector",
    timeout_seconds=10.0,
    idempotent=True,
    is_destructive=False,
)


# ============================================================================
# ADR-0042 (Phase 9): Workspace + Artifact Lifecycle — module-level descriptors
# ============================================================================

WORKSPACE_SNAPSHOT_DESCRIPTOR = CapabilityDescriptor(
    name="workspace.snapshot",
    description="Capture the current state of a workspace (content-addressed).",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"workspace_id": {"type": "string"}},
        "required": ["workspace_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "snapshot_id": {"type": "string"},
            "file_count": {"type": "integer"},
            "total_bytes": {"type": "integer"},
            "files": {"type": "array", "items": {"type": "object"}},
            "idempotent": {"type": "boolean"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=30.0,
    idempotent=True,
    is_destructive=False,
)

WORKSPACE_RESTORE_DESCRIPTOR = CapabilityDescriptor(
    name="workspace.restore",
    description="Restore a snapshot into a target workspace.",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "snapshot_id": {"type": "string"},
            "target_workspace_id": {"type": "string"},
        },
        "required": ["snapshot_id", "target_workspace_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "restored_files": {"type": "integer"},
            "total_bytes": {"type": "integer"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=60.0,
    idempotent=False,
    is_destructive=False,
)

WORKSPACE_PROMOTE_DESCRIPTOR = CapabilityDescriptor(
    name="workspace.promote",
    description="Promote a TTL-bound workspace to permanent (survives the reaper).",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"workspace_id": {"type": "string"}},
        "required": ["workspace_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "promoted": {"type": "boolean"},
            "permanent_resource_id": {"type": "string"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=10.0,
    idempotent=True,
    is_destructive=False,
)

ARTIFACT_CAPTURE_DESCRIPTOR = CapabilityDescriptor(
    name="artifact.capture",
    description=(
        "Capture a file from a workspace as a durable artifact. Computes "
        "SHA-256, records provenance. The file stays in the workspace; "
        "the artifact record is the queryable evidence."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "workspace_id": {"type": "string"},
            "path": {"type": "string", "description": "workspace-relative"},
            "filename": {"type": "string"},
        },
        "required": ["workspace_id", "path", "filename"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "artifact_id": {"type": "string"},
            "sha256": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "filename": {"type": "string"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=10.0,
    idempotent=False,
    is_destructive=False,
)

ARTIFACT_LIST_DESCRIPTOR = CapabilityDescriptor(
    name="artifact.list",
    description="List the principal's artifacts. Metadata only — NEVER file bytes.",
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"objective_id": {"type": "string"}},
    },
    output_schema={
        "type": "object",
        "properties": {
            "artifacts": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

ARTIFACT_RETRIEVE_DESCRIPTOR = CapabilityDescriptor(
    name="artifact.retrieve",
    description=(
        "Retrieve an artifact's metadata + re-verify integrity. If the "
        "file is gone (workspace expired), returns integrity_verified=false."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"artifact_id": {"type": "string"}},
        "required": ["artifact_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "artifact": {"type": "object"},
            "integrity_verified": {"type": "boolean"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=10.0,
    idempotent=True,
    is_destructive=False,
)


# ============================================================================
# ADR-0043 (Phase 10): Media + Delivery — module-level descriptors
# ============================================================================

DELIVERY_STATUS_DESCRIPTOR = CapabilityDescriptor(
    name="delivery.status",
    description=(
        "Check the delivery status of an outbound message. Returns "
        "metadata only — NEVER the message text. Use delivery_id for "
        "a specific delivery, or execution_id for all deliveries "
        "from that execution."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "delivery_id": {"type": "string"},
            "execution_id": {"type": "string"},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "deliveries": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

DELIVERY_RETRY_DESCRIPTOR = CapabilityDescriptor(
    name="delivery.retry",
    description=(
        "Manually trigger a retry of a failed delivery. Resets "
        "next_attempt_at to now; the maintenance loop picks it up. "
        "Cannot bypass max_attempts — once exhausted, the delivery "
        "is terminal failed."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {"delivery_id": {"type": "string"}},
        "required": ["delivery_id"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "retried": {"type": "boolean"},
            "status": {"type": "string"},
            "next_attempt_at": {"type": "string"},
            "reason": {"type": "string"},
        },
    },
    required_permission="capability.invoke:built_in",
    timeout_seconds=10.0,
    idempotent=True,
    is_destructive=False,
)
