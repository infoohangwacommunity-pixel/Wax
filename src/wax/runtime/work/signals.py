"""Runtime signals — the event ledger that makes waiting condition-driven.

Design (ADR-0011): a signal is a named, persisted fact. Emission is a
single INSERT into `runtime_signals`; no fan-out, no in-memory state, no
delivery failures. Waiting is implemented by the WorkRunner's claim query,
which correlates event-wake work items against this ledger. This gives
event waiting the identical crash/lease/retry semantics as time waiting.

Namespaces and trust:
- "interface.*"  — emitted only by the runtime (bridge) when a principal's
  inbound message has been processed. The AI cannot forge "the user
  responded" — that fact belongs to the interface boundary.
- "work.*"       — emitted only by the work runner when work reaches a
  terminal state. The AI cannot forge "this work finished".
- all other names — open for gated emission by intelligence through the
  signal.emit capability (agency → authority → audit like any effect).

This module is runtime-layer; it touches the DB only through the session
it is given.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.runtime.logging import get_logger
from wax.state.work_models import RuntimeSignalRecord

log = get_logger(__name__)

# Namespaces the runtime owns. Intelligence may WAIT on these (waiting on
# "the user replied", "that work finished", or "my approval was granted" is
# legitimate) but may never EMIT them — forging them would let the model
# fake facts that belong to the runtime's authority boundaries.
RESERVED_SIGNAL_PREFIXES = ("interface.", "work.", "approval.")

SIGNAL_NAME_MAX = 160
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{1,159}$")


class SignalNameError(ValueError):
    """Raised when a signal name violates format or namespace policy."""


def validate_signal_name(name: str, *, allow_reserved: bool) -> str:
    """Validate a signal name. Returns the normalized name or raises.

    - format: 2..160 chars, [a-zA-Z0-9._:-], must start alphanumeric
    - namespaced names use dots: "namespace.value"
    - reserved namespaces ("interface.", "work.") are runtime-owned;
      intelligence-supplied names may not use them (allow_reserved=False).
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SignalNameError(
            f"signal name must match [a-zA-Z0-9][a-zA-Z0-9._:-]{{1,159}} (got {name!r})"
        )
    if not allow_reserved:
        for prefix in RESERVED_SIGNAL_PREFIXES:
            if name.startswith(prefix):
                raise SignalNameError(
                    f"signal namespace {prefix!r} is runtime-owned; the "
                    "intelligence may wait on these signals but never emit them"
                )
    return name


class SignalRepository:
    """Data access for runtime_signals. Caller owns transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def emit(
        self,
        name: str,
        *,
        payload: dict | None = None,
        emitted_by: str | None = None,
        at: datetime | None = None,
    ) -> RuntimeSignalRecord:
        """Append one signal to the ledger. Never raises on duplicate
        names — signals are facts, not resources; the same fact may be
        observed twice (each observation is a new row)."""
        now = at or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        record = RuntimeSignalRecord(
            id=str(ULID()),
            name=name,
            payload=payload,
            emitted_at=now,
            emitted_by=(emitted_by or "runtime")[:64],
        )
        self._session.add(record)
        await self._session.flush()
        log.info(
            "signal.emitted",
            signal_id=record.id,
            name=name,
            emitted_by=record.emitted_by,
        )
        return record

    async def has_signal_since(self, name: str, *, after: datetime) -> RuntimeSignalRecord | None:
        """The newest signal with this name emitted strictly after `after`
        (None if none exists). The watermark comparison is strict so a
        wait that begins at instant T is never woken by a signal recorded
        at or before T — waiting is never satisfied retroactively."""
        result = await self._session.execute(
            select(RuntimeSignalRecord)
            .where(
                RuntimeSignalRecord.name == name,
                RuntimeSignalRecord.emitted_at > after,
            )
            .order_by(RuntimeSignalRecord.emitted_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def count_since(self, name: str, *, after: datetime) -> int:
        result = await self._session.execute(
            select(RuntimeSignalRecord.id).where(
                RuntimeSignalRecord.name == name,
                RuntimeSignalRecord.emitted_at > after,
            )
        )
        return len(result.all())

    # --- Lifecycle: deterministic, waiter-safe retention (ADR-0015) -------

    @staticmethod
    def _needed_by_a_waiter():
        """Correlated EXISTS: a PENDING event-wake item whose watermark
        predates the signal can still be woken by it. The watermark lives
        on the work item, so pruning never mutates wait semantics: a signal
        that could fire an existing wait is NEVER deleted, and a waiter
        created after a prune gets watermark=now (it could not have been
        woken by the pruned rows anyway — waiting is never retroactive)."""
        from wax.state.work_models import WAKE_KIND_EVENT, WorkItemRecord

        return (
            select(WorkItemRecord.id)
            .where(
                WorkItemRecord.status == "pending",
                WorkItemRecord.wake_kind == WAKE_KIND_EVENT,
                WorkItemRecord.wake_event == RuntimeSignalRecord.name,
                WorkItemRecord.wake_watermark < RuntimeSignalRecord.emitted_at,
            )
            .exists()
        )

    async def prune(
        self,
        *,
        retention_seconds: float,
        max_rows: int,
        max_delete_per_pass: int = 5000,
    ) -> dict[str, Any]:
        """One deterministic retention pass.

        1. Retention: signals older than `retention_seconds` are deleted —
           unless a pending waiter can still be woken by them.
        2. Bounded storage: when the ledger exceeds `max_rows`, the oldest
           prune-safe rows beyond the bound are deleted (same safety rule),
           capped at `max_delete_per_pass` per pass.

        Returns stats for the audit log. The operation never touches
        signals a live wait still needs, never deletes newer-than-cutoff
        rows beyond the bound arithmetic, and is idempotent per pass.
        """
        from sqlalchemy import delete, func

        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=max(retention_seconds, 0.0))
        stats: dict[str, Any] = {"retention_deleted": 0, "bound_deleted": 0}

        # --- 1. Retention pass -------------------------------------------
        count_result = await self._session.execute(
            select(func.count())
            .select_from(RuntimeSignalRecord)
            .where(
                RuntimeSignalRecord.emitted_at < cutoff,
                ~self._needed_by_a_waiter(),
            )
        )
        retention_count = int(count_result.scalar_one() or 0)
        if retention_count:
            n = min(retention_count, max_delete_per_pass)
            oldest = (
                await self._session.execute(
                    select(RuntimeSignalRecord.id, RuntimeSignalRecord.emitted_at)
                    .where(
                        RuntimeSignalRecord.emitted_at < cutoff,
                        ~self._needed_by_a_waiter(),
                    )
                    .order_by(RuntimeSignalRecord.emitted_at.asc())
                    .limit(n)
                )
            ).all()
            ids = [row.id for row in oldest]
            await self._session.execute(
                delete(RuntimeSignalRecord).where(RuntimeSignalRecord.id.in_(ids))
            )
            stats["retention_deleted"] = len(ids)
            stats["retention_oldest"] = oldest[0].emitted_at.isoformat()
            stats["retention_pending_total"] = retention_count

        # --- 2. Bounded-storage pass ---------------------------------------
        total_result = await self._session.execute(
            select(func.count()).select_from(RuntimeSignalRecord)
        )
        total = int(total_result.scalar_one() or 0)
        if total > max_rows:
            excess = total - max_rows
            n = min(excess, max_delete_per_pass)
            oldest = (
                await self._session.execute(
                    select(RuntimeSignalRecord.id)
                    .where(~self._needed_by_a_waiter())
                    .order_by(RuntimeSignalRecord.emitted_at.asc())
                    .limit(n)
                )
            ).all()
            ids = [row.id for row in oldest]
            if ids:
                await self._session.execute(
                    delete(RuntimeSignalRecord).where(RuntimeSignalRecord.id.in_(ids))
                )
            stats["bound_deleted"] = len(ids)
            stats["ledger_total"] = total
        # `total` was counted AFTER the retention deletes committed in
        # this session, so it already excludes retention_deleted —
        # subtracting it again would report a negative ledger (a defect
        # the live probe surfaced: ledger_total_after=-5).
        stats["ledger_total_after"] = total - stats["bound_deleted"]
        return stats
