"""ApprovalService — the generic human-authorization primitive.

WAX philosophy: the AI has agency, the runtime has sovereignty. Some
actions cross a line where the runtime must have an explicit human YES
before anything happens. Before this service, the agency gate could only
DENY such actions honestly ("no human-approval workflow exists") — the
loop was closed but the environment was not actually capable of
open-ended work that requires consent.

The primitive (generic; every domain-specific approval flow is a
composition on top):

    AI requests action
      ↓ agency gate decides: human authorization required
      ↓ runtime creates/returns a PENDING APPROVAL (idempotent by
        fingerprint; expires; audited; announced on the event ledger)
      ↓ the human decides through their own authority path
        (interface adapter → RuntimeBridge.submit_approval_decision)
      ↓ approved / denied / expired / cancelled
      ↓ an APPROVED approval authorizes EXACTLY ONE re-attempt of the
        SAME action (fingerprint match + one-time consumption)

Security properties (each enforced here, not by prompts):
- the AI cannot create approvals on demand — creation happens inside the
  agency gate when the RUNTIME decides approval is needed;
- the AI cannot decide — decisions authenticate a human principal via
  the interface credential path; there is no approve capability;
- replay is impossible — consumption is one-time and fingerprint-bound;
- scope creep is impossible — mutated inputs produce a new fingerprint;
- staleness is impossible — pending approvals expire and are swept.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.core.exceptions import WaxStateConflictError
from wax.observability.audit import record_audit_event
from wax.runtime.logging import get_logger
from wax.state.approval_models import (
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_DENIED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    PendingApprovalRecord,
)

log = get_logger(__name__)

# How long a pending approval stays decidable. Policy, not mechanism —
# configurable via WAX_APPROVAL_EXPIRY_SECONDS.
DEFAULT_EXPIRY_SECONDS = 86400.0

# Cap on the safe summary size (evidence for the human, not a data dump).
_SUMMARY_VALUE_MAX = 120
_SUMMARY_KEYS_MAX = 8


def fingerprint_request(
    principal_id: str,
    capability_name: str,
    inputs: dict[str, Any] | None,
) -> str:
    """Stable fingerprint of the exact request being authorized.

    Canonical JSON (sorted keys, compact separators) → SHA-256. Two
    identical requests share a fingerprint; ANY change to the inputs
    (even added whitespace at the JSON level) changes it — approvals
    authorize the request the human saw, nothing else.
    """
    canonical = json.dumps(
        {"p": principal_id, "c": capability_name, "i": inputs or {}},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_scope_summary(inputs: dict[str, Any] | None) -> dict[str, str]:
    """A SAFE summary of the requested action's inputs for the human.

    Values are truncated; nested structures are summarized; secrets never
    belong here (capability inputs are validated not to contain secrets,
    and the summary truncates aggressively regardless).
    """
    summary: dict[str, str] = {}
    if not inputs:
        return summary
    for key in list(inputs.keys())[:_SUMMARY_KEYS_MAX]:
        value = inputs[key]
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) > _SUMMARY_VALUE_MAX:
            text = text[:_SUMMARY_VALUE_MAX] + "…"
        summary[str(key)[:64]] = text
    return summary


def _pending_insert(dialect_name: str):
    """A dialect-specific INSERT … ON CONFLICT DO NOTHING targeting the
    partial unique index — the atomic create-or-ignore primitive.

    Both production dialects (postgresql, sqlite) support the identical
    conflict target: (principal_id, request_fingerprint) WHERE status =
    'pending'. Any other dialect gets a plain insert (the database will
    raise on conflict rather than silently double-creating).
    """
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy import insert

    if dialect_name in ("postgresql", "sqlite"):
        return insert(PendingApprovalRecord).on_conflict_do_nothing(
            index_elements=["principal_id", "request_fingerprint"],
            index_where=text("status = 'pending'"),
        )
    return insert(PendingApprovalRecord)


class ApprovalDecisionError(Exception):
    """A decision request cannot be honored. Message is caller-safe."""


class ApprovalService:
    """Data access + lifecycle rules for pending approvals."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- Creation (called by the runtime's agency gate, never by the AI) --

    async def create_or_get_pending(
        self,
        *,
        principal_id: str,
        capability_name: str,
        action_kind: str,
        inputs: dict[str, Any] | None,
        requested_by_execution_id: str | None,
        expires_in_seconds: float = DEFAULT_EXPIRY_SECONDS,
    ) -> tuple[PendingApprovalRecord, bool]:
        """Idempotently create a pending approval for this exact request.

        If an equivalent PENDING approval already exists, it is returned
        (created=False) — re-requesting the same action never multiplies
        approval rows or notifications.
        """
        fp = fingerprint_request(principal_id, capability_name, inputs)

        existing = await self.find_pending(principal_id, fp)
        if existing is not None:
            return existing, False

        now = datetime.now(UTC)
        record_id = str(ULID())
        # CV-11 fix: creation is a single ATOMIC create-or-ignore against
        # the partial unique index
        # uq_pending_approvals_principal_fp_pending. Two concurrent gate
        # passes (live bridge + work handler, or two replicas) can both
        # observe "no pending row"; the DATABASE decides the winner and
        # the loser sees rowcount 0 and returns the winner's row. One
        # approval, one notification, no duplicate authority — no
        # read-then-write race, no exception-path transaction cleanup.
        stmt = _pending_insert(self._session.bind.dialect.name).values(
            id=record_id,
            principal_id=principal_id,
            requested_by_execution_id=requested_by_execution_id,
            capability_name=capability_name[:128],
            action_kind=action_kind[:64],
            scope_summary=build_scope_summary(inputs),
            request_fingerprint=fp,
            status=STATUS_PENDING,
            requested_at=now,
            expires_at=now + timedelta(seconds=expires_in_seconds),
            created_at=now,
            updated_at=now,
        )
        result = await self._session.execute(stmt)
        if result.rowcount == 1:
            record = await self.get(record_id)
            assert record is not None  # inserted in this transaction
            await record_audit_event(
                self._session,
                actor_principal_id=principal_id,
                actor_kind="system",
                event_kind="approval.requested",
                outcome="success",
                payload={
                    "approval_id": record.id,
                    "capability": capability_name,
                    "action_kind": action_kind,
                    "expires_at": record.expires_at.isoformat(),
                    "execution_id": requested_by_execution_id,
                },
            )
            log.info(
                "approval.requested",
                approval_id=record.id,
                principal_id=principal_id,
                capability=capability_name,
            )
            return record, True

        # Lost the creation race — the winner's row is the honest answer.
        existing = await self.find_pending(principal_id, fp)
        if existing is not None:
            return existing, False
        raise WaxStateConflictError("approval creation conflict and no pending row visible; retry")

    # --- Lookups -----------------------------------------------------------

    async def get(self, approval_id: str) -> PendingApprovalRecord | None:
        return await self._session.get(PendingApprovalRecord, approval_id)

    async def find_pending(
        self, principal_id: str, fingerprint: str
    ) -> PendingApprovalRecord | None:
        result = await self._session.execute(
            select(PendingApprovalRecord)
            .where(
                PendingApprovalRecord.principal_id == principal_id,
                PendingApprovalRecord.request_fingerprint == fingerprint,
                PendingApprovalRecord.status == STATUS_PENDING,
            )
            .order_by(PendingApprovalRecord.requested_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def find_approved_unconsumed(
        self,
        principal_id: str,
        fingerprint: str,
        *,
        approval_ttl_seconds: float = DEFAULT_EXPIRY_SECONDS,
    ) -> PendingApprovalRecord | None:
        """An approved, unconsumed, unexpired approval for this request.

        The staleness window is the deployment's configured approval TTL
        (WAX_APPROVAL_EXPIRY_SECONDS), not a module constant — callers pass
        settings.approval_expiry_seconds so the runtime has one expiry law."""
        now = datetime.now(UTC)
        result = await self._session.execute(
            select(PendingApprovalRecord)
            .where(
                PendingApprovalRecord.principal_id == principal_id,
                PendingApprovalRecord.request_fingerprint == fingerprint,
                PendingApprovalRecord.status == STATUS_APPROVED,
                PendingApprovalRecord.consumed_at.is_(None),
            )
            .order_by(PendingApprovalRecord.decided_at.desc())
            .limit(1)
        )
        record = result.scalar_one_or_none()
        if record is not None:
            # Approved approvals do not hard-expire, but an approval
            # decided long before use is suspect; keep the window honest.
            decided = record.decided_at
            if decided is not None:
                if decided.tzinfo is None:
                    decided = decided.replace(tzinfo=UTC)
                if (now - decided) > timedelta(seconds=approval_ttl_seconds):
                    return None
        return record

    async def list_for_principal(
        self,
        principal_id: str,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> list[PendingApprovalRecord]:
        stmt = (
            select(PendingApprovalRecord)
            .where(PendingApprovalRecord.principal_id == principal_id)
            .order_by(PendingApprovalRecord.requested_at.desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(PendingApprovalRecord.status == status)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # --- Consumption (one-time use — the replay guard) ----------------------

    async def consume(self, approval_id: str, *, execution_id: str | None) -> bool:
        """Consume an approved approval. Returns False if it was already
        consumed (replay attempt) or is not in consumable state.

        The claim is a SINGLE CONDITIONAL UPDATE
        (… WHERE status='approved' AND consumed_at IS NULL): the rowcount
        decides who won. A read-then-write sequence here would let two
        concurrent gate passes (live bridge + work handler, or two
        replicas) both observe consumed_at IS NULL and both execute —
        breaking the exactly-once-per-human-decision guarantee."""
        result = await self._session.execute(
            update(PendingApprovalRecord)
            .where(
                PendingApprovalRecord.id == approval_id,
                PendingApprovalRecord.status == STATUS_APPROVED,
                PendingApprovalRecord.consumed_at.is_(None),
            )
            .values(
                consumed_at=datetime.now(UTC),
                consumed_by_execution_id=execution_id,
            )
        )
        if result.rowcount != 1:
            return False

        record = await self.get(approval_id)
        if record is None:  # vanished mid-flight; the UPDATE already decided
            return False
        await record_audit_event(
            self._session,
            actor_principal_id=record.principal_id,
            actor_kind="system",
            event_kind="approval.consumed",
            outcome="success",
            payload={
                "approval_id": record.id,
                "capability": record.capability_name,
                "execution_id": execution_id,
            },
        )
        log.info("approval.consumed", approval_id=record.id)
        return True

    # --- Decisions (called ONLY through the human authority path) -----------

    async def decide(
        self,
        approval_id: str,
        *,
        decided_by: str,
        approve: bool,
        note: str | None = None,
    ) -> PendingApprovalRecord:
        """Record the human's decision. Raises ApprovalDecisionError on
        any illegitimate request (wrong owner, wrong state) — the caller
        translates that into an honest response."""
        record = await self.get(approval_id)
        if record is None:
            raise ApprovalDecisionError("no such approval")

        # OWNERSHIP: only the human whose action is being authorized may
        # decide. This is the authority boundary; it is not negotiable.
        if record.principal_id != decided_by:
            raise ApprovalDecisionError("approval does not belong to this principal")

        if record.status != STATUS_PENDING:
            raise ApprovalDecisionError(f"approval is {record.status}, not pending")

        # EXPIRY: a request whose validity window has closed is dead even
        # if the maintenance sweep has not reached it yet. Approving an
        # expired request would let a stale authorization land.
        expires_at = record.expires_at
        if expires_at is not None:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at < datetime.now(UTC):
                raise ApprovalDecisionError("approval has expired")

        record.status = STATUS_APPROVED if approve else STATUS_DENIED
        record.decided_by = decided_by
        record.decided_at = datetime.now(UTC)
        record.decision_note = (note or "")[:2000] or None
        await self._session.flush()

        await record_audit_event(
            self._session,
            actor_principal_id=decided_by,
            actor_kind="human",
            event_kind="approval.decided",
            outcome="success",
            payload={
                "approval_id": record.id,
                "decision": record.status,
                "capability": record.capability_name,
                "note": (note or "")[:200],
            },
        )
        log.info(
            "approval.decided",
            approval_id=record.id,
            decision=record.status,
            decided_by=decided_by,
        )
        return record

    async def cancel(self, approval_id: str, *, by_principal_id: str) -> PendingApprovalRecord:
        """The requester's principal withdraws their own pending request."""
        record = await self.get(approval_id)
        if record is None:
            raise ApprovalDecisionError("no such approval")
        if record.principal_id != by_principal_id:
            raise ApprovalDecisionError("approval does not belong to this principal")
        if record.status != STATUS_PENDING:
            raise ApprovalDecisionError(f"approval is {record.status}, not pending")
        record.status = STATUS_CANCELLED
        record.decided_at = datetime.now(UTC)
        record.decision_note = "cancelled by requester"
        await self._session.flush()
        await record_audit_event(
            self._session,
            actor_principal_id=by_principal_id,
            actor_kind="system",
            event_kind="approval.cancelled",
            outcome="success",
            payload={"approval_id": record.id, "capability": record.capability_name},
        )
        log.info("approval.cancelled", approval_id=record.id)
        return record

    # --- Expiry sweep (runtime-owned lifecycle) ------------------------------

    async def expire_due(self) -> list[PendingApprovalRecord]:
        """Expire every pending approval past its deadline. Returns the
        expired rows (caller commits). Deterministic, honest cleanup."""
        now = datetime.now(UTC)
        result = await self._session.execute(
            select(PendingApprovalRecord).where(
                PendingApprovalRecord.status == STATUS_PENDING,
                PendingApprovalRecord.expires_at <= now,
            )
        )
        expired = list(result.scalars().all())
        for record in expired:
            record.status = STATUS_EXPIRED
            await record_audit_event(
                self._session,
                actor_principal_id=record.principal_id,
                actor_kind="system",
                event_kind="approval.expired",
                outcome="success",
                payload={
                    "approval_id": record.id,
                    "capability": record.capability_name,
                },
            )
        if expired:
            log.info("approval.expired_batch", count=len(expired))
        return expired
