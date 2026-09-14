"""Capability-invocation idempotency ledger (CV-19).

`CapabilityInvocationRequest.idempotency_key` was declared in the
contract ("the runtime validates the idempotency key has not been used")
while nothing read it — a false mechanism at the SOLE effect point.

This module makes the promise real as a GENERIC mechanism:

- A claim row (principal, capability, key) is inserted atomically
  (INSERT .. ON CONFLICT DO NOTHING against a full unique index) BEFORE
  the implementation runs. Two concurrent identical requests — live
  bridge, work handler, or two replicas — cannot both claim.
- A `succeeded` claim records the response. A replay returns the
  RECORDED outcome (`idempotent_replay=True`), so a duplicate request
  can never double-execute an effect.
- A `failed` claim can be taken over by an identical retry: the first
  attempt produced no recorded outcome, so re-execution is honest.
- An `executing` claim holds a lease. A lease expiry means the claiming
  process died between claim and completion — the effect state is
  UNKNOWN, so an identical request may take the claim over and
  re-execute (at-least-once, the same honest semantics the durable-work
  queue documents). Fencing is inherent: only the takeover wins the
  conditional UPDATE.

The ledger never deletes rows: replay evidence is audit evidence.

This is infrastructure: it knows nothing about what the capability is
for. Capabilities do not opt in or out — the CALLER (the intelligence,
via the request contract) decides whether a request carries a key.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ulid import ULID

from wax.authority.approvals import fingerprint_request
from wax.runtime.logging import get_logger
from wax.state.capability_models import CapabilityInvocationRecord
from wax.state.engine import db_session

log = get_logger(__name__)


def _aware(dt: datetime) -> datetime:
    """Normalize a DB-read timestamp to aware UTC.

    PostgreSQL timestamptz returns aware datetimes; SQLite stores
    DateTime(timezone=True) WITHOUT the offset and returns naive
    datetimes. The runtime only ever writes UTC, so a naive value read
    from the database IS UTC by definition — asserting tzinfo at the
    comparison boundary keeps lease arithmetic dialect-honest.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class Verdict(StrEnum):
    """What the caller must do with the claim."""

    CLAIMED = "claimed"      # proceed; you own the claim
    REPLAY = "replay"        # do NOT execute; return the recorded outcome
    EXECUTING = "executing"  # do NOT execute; an identical request is live
    TAKEOVER_LOST = (
        "takeover_lost"  # do NOT execute; another retry won the takeover
    )


class ClaimResult:
    """The claim verdict plus the row (for replay) when relevant."""

    __slots__ = ("record", "verdict")

    def __init__(self, verdict: Verdict, record: CapabilityInvocationRecord) -> None:
        self.verdict = verdict
        self.record = record


def _claim_insert(dialect_name: str):
    """Atomic create-or-ignore against the full unique index on
    (principal_id, capability_name, idempotency_key)."""
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy import insert

    if dialect_name in ("postgresql", "sqlite"):
        return insert(CapabilityInvocationRecord).on_conflict_do_nothing(
            index_elements=["principal_id", "capability_name", "idempotency_key"],
        )
    return insert(CapabilityInvocationRecord)


async def claim_invocation(
    *,
    principal_id: str,
    capability_name: str,
    idempotency_key: str,
    inputs: dict | None,
    claim_seconds: float,
) -> ClaimResult:
    """Claim (or classify) an idempotent capability invocation.

    Opens its own database session: the claim must be durable even if
    the caller's surrounding transaction later rolls back — otherwise a
    rolled-back claim would let a duplicate execute twice.
    """
    fingerprint = fingerprint_request(principal_id, capability_name, inputs)
    async with db_session() as session:
        now = datetime.now(UTC)

        stmt = _claim_insert(session.bind.dialect.name).values(
            id=str(ULID()),
            principal_id=principal_id,
            capability_name=capability_name[:255],
            idempotency_key=idempotency_key[:512],
            request_fingerprint=fingerprint,
            status="executing",
            claim_expires_at=now + timedelta(seconds=claim_seconds),
            created_at=now,
            updated_at=now,
        )
        result = await session.execute(stmt)
        if result.rowcount == 1:
            await session.commit()
            record = await _find(session, principal_id, capability_name, idempotency_key)
            log.info(
                "capability.idempotency_claimed",
                capability=capability_name,
                principal_id=principal_id,
                claim_id=record.id if record else None,
            )
            return ClaimResult(Verdict.CLAIMED, record)

        # Lost the insert race — an earlier claim exists; classify it.
        record = await _find(session, principal_id, capability_name, idempotency_key)
        if record is None:
            # Conflict reported but row not visible (edge of the window);
            # treat as executing — the honest answer is "do not run now".
            await session.rollback()
            return ClaimResult(Verdict.EXECUTING, None)

        if record.status == "succeeded":
            await session.commit()
            return ClaimResult(Verdict.REPLAY, record)

        takeover_eligible = record.status == "failed" or (
            record.status == "executing"
            and record.claim_expires_at is not None
            and _aware(record.claim_expires_at) <= now
        )
        if not takeover_eligible:
            await session.commit()
            return ClaimResult(Verdict.EXECUTING, record)

        # Take over: a failed attempt or an expired lease has no recorded
        # outcome, so re-execution is honest (at-least-once). The
        # conditional UPDATE is the fence — only one retry wins.
        # synchronize_session=False keeps this a pure database-side
        # fence: the ORM's default "evaluate" strategy would re-run the
        # WHERE clause in Python against in-session rows, comparing the
        # DB's naive timestamps against aware ones (and the row is
        # re-fetched via _find below anyway).
        from sqlalchemy import update

        stmt = (
            update(CapabilityInvocationRecord)
            .where(CapabilityInvocationRecord.id == record.id)
            .where(
                CapabilityInvocationRecord.status.in_(["failed", "executing"])
            )
            .where(
                (CapabilityInvocationRecord.status == "failed")
                | (CapabilityInvocationRecord.claim_expires_at <= now)
            )
            .values(
                status="executing",
                claim_expires_at=now + timedelta(seconds=claim_seconds),
                error=None,
                response_json=None,
                completed_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        taken = await session.execute(stmt)
        await session.commit()
        if taken.rowcount == 1:
            refreshed = await _find(session, principal_id, capability_name, idempotency_key)
            log.info(
                "capability.idempotency_takeover",
                capability=capability_name,
                principal_id=principal_id,
                claim_id=record.id,
                previous_status=record.status,
            )
            return ClaimResult(Verdict.CLAIMED, refreshed)
        return ClaimResult(Verdict.TAKEOVER_LOST, record)


async def complete_success(outputs: dict, claim_id: str | None) -> None:
    """Record the outcome on a claim this process owns."""
    if claim_id is None:
        return
    async with db_session() as session:
        from sqlalchemy import select

        record = (
            await session.execute(
                select(CapabilityInvocationRecord).where(
                    CapabilityInvocationRecord.id == claim_id
                )
            )
        ).scalar_one_or_none()
        if record is None:
            return
        record.status = "succeeded"
        record.response_json = json.dumps(outputs, default=str)
        record.error = None
        record.claim_expires_at = None
        record.completed_at = datetime.now(UTC)
        record.updated_at = record.completed_at
        await session.commit()


async def complete_failure(error: str, claim_id: str | None) -> None:
    """Record a failed attempt on a claim this process owns (a later
    identical retry may take the claim over)."""
    if claim_id is None:
        return
    async with db_session() as session:
        from sqlalchemy import select

        record = (
            await session.execute(
                select(CapabilityInvocationRecord).where(
                    CapabilityInvocationRecord.id == claim_id
                )
            )
        ).scalar_one_or_none()
        if record is None:
            return
        record.status = "failed"
        record.error = (error or "")[:5000]
        record.claim_expires_at = None
        record.completed_at = datetime.now(UTC)
        record.updated_at = record.completed_at
        await session.commit()


async def _find(
    session, principal_id: str, capability_name: str, idempotency_key: str
) -> CapabilityInvocationRecord | None:
    from sqlalchemy import select

    return (
        await session.execute(
            select(CapabilityInvocationRecord)
            .where(CapabilityInvocationRecord.principal_id == principal_id)
            .where(CapabilityInvocationRecord.capability_name == capability_name[:255])
            .where(CapabilityInvocationRecord.idempotency_key == idempotency_key[:512])
        )
    ).scalar_one_or_none()
