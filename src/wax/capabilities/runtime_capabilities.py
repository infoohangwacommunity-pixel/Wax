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
    sync_active_for_execution,
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
        from wax.runtime.work.repository import WorkRepository
        from wax.runtime.work.signals import (
            SignalNameError,
            validate_signal_name,
        )
        from wax.state.work_models import WAKE_KIND_EVENT, WAKE_KIND_TIME

        payload = inputs.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("payload object is required")
        capability_name = payload.get("capability_name")
        if not capability_name or not isinstance(capability_name, str):
            raise ValueError("payload.capability_name is required")

        # Honest early feedback: refuse to schedule work whose capability
        # does not exist (availability may still change by wake time).
        try:
            descriptor, _impl = services.capability_registry.get(capability_name)
        except Exception as e:
            raise ValueError(f"Unknown capability for scheduled work: {capability_name}") from e

        kind = inputs.get("kind") or "capability"
        if kind != "capability":
            raise ValueError(f"Unsupported work kind: {kind!r} (only 'capability')")

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

        if descriptor.is_destructive:
            # Destructive work is schedulable: at wake time the agency gate
            # routes it into the human-approval primitive (a pending
            # approval is created honestly, the attempt fails without
            # consuming anything, and the human's approval authorizes the
            # retry/requeue exactly once). The authority boundary moved
            # from "refuse to schedule" to "refuse to run without an
            # explicit human YES" — the stronger, generic guarantee.
            pass

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

        async with db_session() as session:
            repo = WorkRepository(session)
            item = await repo.schedule(
                kind=kind,
                payload={
                    "capability_name": capability_name,
                    "inputs": payload.get("inputs") or {},
                },
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
            await sync_waiting_for_execution(
                session, ctx.request_id or ctx.execution_id
            )
            await session.commit()

        return {
            "work_id": item.id,
            "status": item.status,
            "wake_kind": item.wake_kind,
            "wake_event": item.wake_event,
            "wake_at": item.wake_at.isoformat(),
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "capability_name": capability_name,
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
                        "the attached interface does not accept a plain-text "
                        "delivery right now"
                    )
                    raise ValueError(
                        f"Delivery refused by the attached {interface_kind} "
                        f"interface's delivery policy: {note}"
                    )

        await delivery.send(interface_kind, recipient_id, text)
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
            "path": record.uri,
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

    async def approval_cancel_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:
        from wax.authority.approvals import ApprovalDecisionError, ApprovalService

        approval_id = inputs.get("approval_id")
        if not approval_id or not isinstance(approval_id, str):
            raise ValueError("approval_id is required")
        async with db_session() as session:
            try:
                await ApprovalService(session).cancel(
                    approval_id, by_principal_id=ctx.principal_id
                )
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
    log.info("capability.runtime_registered", count=19)


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
MEMORY_LINK_KINDS = ("supports", "contradicts", "derived_from", "related_to")

MEMORY_STORE_DESCRIPTOR = CapabilityDescriptor(
    name="memory.store",
    description="Persist a memory for the current principal (structured "
    "evidence, not a frozen category). Pass expires_at for anything that "
    "should be forgotten automatically. Pass supersedes=<memory_id> when "
    "this record REPLACES an older active memory of the same principal "
    "(revision: the old record stays for audit but leaves retrieval). "
    "Optional importance (0-1) weights retrieval; observed_at records "
    "when the fact was observed (vs written); links=[{memory_id, kind}] "
    "adds typed edges (supports/contradicts/derived_from/related_to) to "
    "existing memories.",
    version="1.2.0",
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
            "links": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "supports",
                                "contradicts",
                                "derived_from",
                                "related_to",
                            ],
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
            "superseded": {"type": "string"},
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
        "properties": {"memory_id": {"type": "string"}, "forgotten": {"type": "boolean"}},
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
            observed_at = datetime.fromisoformat(
                str(inputs["observed_at"]).replace("Z", "+00:00")
            )
        except ValueError as e:
            raise ValueError(
                f"observed_at is not a valid ISO-8601 datetime: {e}"
            ) from e

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
                raise ValueError(
                    f"No active memory {entry['memory_id']} to link to"
                )
            if target.principal_id != ctx.principal_id:
                raise ValueError(
                    "links target another principal's memory "
                    f"({entry['memory_id']})"
                )

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
                linked.append(
                    {"memory_id": edge.to_memory_id, "kind": edge.kind}
                )
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
            raise ValueError(
                f"memory {memory_id} is {memory.status} and cannot be forgotten"
            )
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

        # Structured provenance edges (ADR-0022): the consolidation is
        # derived_from every source — queryable, not just JSON-in-content.
        # Created BEFORE supersession: sources must still be ACTIVE for
        # edges to be valid.
        for sid in source_ids:
            await repo.link(
                record.id, sid, "derived_from", execution_id=ctx.execution_id
            )
        superseded_ids: list[str] = []
        if supersede_sources:
            for sid in source_ids:
                if await repo.supersede(sid, record.id):
                    superseded_ids.append(sid)
        await session.commit()

    return {
        "memory_id": record.id,
        "consolidated_count": len(source_ids),
        "superseded_ids": superseded_ids,
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
        records = await repo.list_for_principal(
            ctx.principal_id, status=status, limit=limit
        )
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
            raise ValueError(
                f"Objective {objective_id} is {target.status} and cannot be resumed"
            )

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
        raise ValueError(
            f"status must be one of {list(_VALID_OBJECTIVE_CLOSE_STATUSES)}"
        )
    evidence = inputs.get("evidence")
    if not evidence or not isinstance(evidence, str):
        raise ValueError("evidence is required: the runtime records WHY with the status")

    async with db_session() as session:
        repo = ObjectiveRepository(session)
        record = await repo.get(objective_id)
        if record is None or record.principal_id != ctx.principal_id:
            raise ValueError(f"No such objective for this principal: {objective_id}")
        if record.status in ("succeeded", "failed", "cancelled", "abandoned"):
            raise ValueError(
                f"Objective {objective_id} is already terminal ({record.status})"
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
                f"Cannot transition objective {objective_id} from "
                f"{record.status} to {status}"
            )
        await session.commit()

    return {
        "objective_id": objective_id,
        "status": status,
        "history_rows_closed": closed,
    }
