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
from wax.runtime.logging import get_logger
from wax.state.engine import db_session

log = get_logger(__name__)

# The 24-hour customer service window: inside it, a plain text reply is
# permitted by Meta policy; outside it, an approved template would be
# required. WAX has no registered templates, so the runtime reports that
# constraint truthfully instead of faking a send.
CUSTOMER_SERVICE_WINDOW = timedelta(hours=24)

# Interface kind → credential kind (mirrors the bridge's identity mapping).
_INTERFACE_CREDENTIAL_KIND: dict[str, str] = {
    "whatsapp": "whatsapp_phone",
    "web": "web_session",
    "telegram": "telegram_chat",
    "api": "api_key",
}

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
        "Send a text message to the requesting principal on an attached "
        "interface (default: whatsapp). Enforces identity ownership and "
        "Meta's 24-hour customer service window."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "recipient_id": {"type": "string"},
            "text": {
                "type": "string",
                "maxLength": 12000,
                "description": "Chunks over 4096 chars are delivered as marked parts",
            },
            "interface_kind": {"type": "string", "default": "whatsapp"},
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
            # Honest constraint: scheduled destructive work would hit the
            # agency gate at wake time and could never run.
            raise ValueError(
                "Destructive capabilities cannot be scheduled: no "
                "human-approval workflow exists yet"
            )

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
        from wax.state.bridge_models import ProcessedMessageRecord
        from wax.state.identity_models import PrincipalCredential

        interface_kind = inputs.get("interface_kind") or "whatsapp"
        if interface_kind not in _INTERFACE_CREDENTIAL_KIND:
            raise ValueError(f"Unknown interface kind: {interface_kind!r}")
        credential_kind = _INTERFACE_CREDENTIAL_KIND[interface_kind]

        recipient_id = inputs.get("recipient_id")
        text = inputs.get("text")
        if not recipient_id or not isinstance(recipient_id, str):
            raise ValueError("recipient_id is required")
        if not text or not isinstance(text, str):
            raise ValueError("text is required")
        if len(text) > 12000:
            raise ValueError("text exceeds 12000 characters")

        delivery = services.delivery
        if not delivery.has(interface_kind):
            raise ValueError(
                f"No delivery interface attached for {interface_kind!r}; the "
                "runtime cannot send messages right now (an honest "
                "constraint, not a success)."
            )

        async with db_session() as session:
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

            # 2. Meta 24-hour window — evidence from the message ledger.
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
            if last_inbound is None or (now - last_inbound) > CUSTOMER_SERVICE_WINDOW:
                raise ValueError(
                    "Outside the 24-hour customer service window: Meta "
                    "requires an approved template message, and WAX has no "
                    "registered templates. Ask the user to message WAX first."
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
    log.info("capability.runtime_registered", count=14)


# --- memory.* (memory as a mechanism, not an AI chore) --------------------
#
# The runtime owns memory storage; the AI owns interpretation. Before
# these capabilities existed, the AI could not deliberately persist a
# fact ("user is preparing for WAEC physics"), search its own evidence,
# or honor a withdrawal request ("forget that") — only implicit episodic
# writes happened. Memory operations now cross the SAME gate chain as
# every other effect: agency → budget → authority → invoker → audit.

MEMORY_KINDS = ("episodic", "semantic", "procedural", "contextual", "external")

MEMORY_STORE_DESCRIPTOR = CapabilityDescriptor(
    name="memory.store",
    description="Persist a memory for the current principal (structured "
    "evidence, not a frozen category). Pass expires_at for anything that "
    "should be forgotten automatically. Pass supersedes=<memory_id> when "
    "this record REPLACES an older active memory of the same principal "
    "(revision: the old record stays for audit but leaves retrieval).",
    version="1.1.0",
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
    required_permission="capability.invoke:built_in",
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
    required_permission="capability.invoke:built_in",
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
    required_permission="capability.invoke:built_in",
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
    required_permission="capability.invoke:built_in",
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

    supersedes_id = inputs.get("supersedes")
    if supersedes_id is not None and not (isinstance(supersedes_id, str) and supersedes_id):
        raise ValueError("supersedes must be a memory_id string")

    async with db_session() as session:
        repo = MemoryRepository(session)

        # Revision path: verify the target BEFORE creating the replacement
        # so a bad supersedes request creates nothing (no orphan evidence).
        if supersedes_id:
            old = await repo.get(supersedes_id)
            if old is None or old.status != "active":
                raise ValueError(f"No active memory {supersedes_id} to supersede")
            if old.principal_id != ctx.principal_id:
                raise ValueError("supersedes targets another principal's memory")

        record = await repo.create(
            MemoryCreate(
                principal_id=ctx.principal_id,
                kind=MemoryKind(kind),
                content=content,
                provenance="model_observation",
                source_execution_id=ctx.execution_id,
                confidence=float(confidence) if confidence is not None else None,
                expires_at=expires_at,
                summary=summary[:2000] if summary else None,
            )
        )
        if supersedes_id:
            await repo.supersede(supersedes_id, record.id)
        await session.commit()

    return {
        "memory_id": record.id,
        "kind": record.kind,
        "superseded": supersedes_id,
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
        if memory is None or memory.status != "active":
            raise ValueError(f"No active memory {memory_id}")
        if memory.principal_id != ctx.principal_id:
            # Ownership boundary: a principal cannot forget another's memory.
            raise ValueError("memory_id belongs to a different principal")
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
