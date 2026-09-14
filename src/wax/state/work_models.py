"""Persistence for the durable work runtime (Phase R / V).

A WorkItem is the runtime's representation of UNFINISHED WORK that must
survive process restarts, crashes, user disappearance, and long waits.
It is deliberately schema-light:

- kind: which HANDLER should run this item (a runtime mechanism name,
  e.g. "capability") — not a domain category.
- payload: opaque JSON interpreted by the handler (evidence, not ontology).
- wake condition: WHEN the work may run. Two generic shapes exist:
  * time   — wake when the clock reaches wake_at (the runtime owns time).
  * event  — wake when a named runtime signal is emitted after the item's
    wake_watermark (the runtime owns the event ledger; see
    RuntimeSignalRecord). Signals are broadcast: every waiter on a name
    wakes; each consumes them independently via its own watermark.
- lease: claim/lease columns make claiming safe across workers and across
  restarts (a worker that dies releases its lease by expiry).

Nothing here knows about timers, reminders, study sessions, or any use
case. A reminder is one composition: work.schedule → capability handler →
message.send. "Continue when the user next messages me" is another:
wake_event="interface.message:<principal>" → capability handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin

# Wake-condition kinds. A closed set enforced by the repository; adding a
# new kind is a deliberate architectural act (it changes claim semantics).
WAKE_KIND_TIME = "time"
WAKE_KIND_EVENT = "event"
VALID_WAKE_KINDS = frozenset({WAKE_KIND_TIME, WAKE_KIND_EVENT})


class WorkItemRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A unit of durable work with a wake condition and a lifecycle."""

    __tablename__ = "work_items"
    __table_args__ = (
        Index("ix_work_items_status_wake", "status", "wake_at"),
        Index("ix_work_items_principal", "principal_id"),
        Index("ix_work_items_execution", "execution_id"),
        Index("ix_work_items_status_expires", "status", "expires_at"),
    )

    # Which handler runs this item. Runtime mechanism name — currently
    # "capability" (wake and invoke a registered capability). Free text so
    # new handler kinds absorb new work shapes without migration.
    kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # Lifecycle: pending → leased → running → succeeded | failed(→pending)
    # | dead | cancelled
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", server_default="pending"
    )

    # The human this work belongs to (identity continuity across time).
    principal_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # The originating execution (objective continuity / traceability).
    execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)

    # Opaque work description for the handler. NEVER secrets.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # What the handler produced (if it succeeded).
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # When the work should run (UTC). The runtime owns time.
    # For time-wake items: the wake moment. For event-wake items: the
    # earliest claim moment (a floor, e.g. "not before"), defaulting to the
    # scheduling instant.
    wake_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Earliest claim time (wake_at initially; now+backoff on retry).
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # --- Wake condition (generic waiting, not just timers) ---------------
    # "time" (default) or "event". Event items additionally carry the name
    # of the signal that satisfies the condition and a watermark: only
    # signals emitted STRICTLY AFTER the watermark can wake the item, so a
    # signal that predates the wait never fires it (no retroactive wakes).
    wake_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=WAKE_KIND_TIME
    )
    wake_event: Mapped[str | None] = mapped_column(String(160), nullable=True)
    wake_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional deadline for the WAIT itself: if the condition has not been
    # met by this time, the item honestly dies ("condition not met") rather
    # than waiting forever. Applies while pending; once woken, normal retry
    # semantics apply.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )

    # Claim lease (crash safety across workers/restarts).
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RuntimeSignalRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A persisted runtime signal — the event ledger for durable waiting.

    A signal is a named, timestamped fact that something happened in the
    runtime: an interface message arrived, a work item reached a terminal
    state, an external integration reported a change. Emission is an
    INSERT; nothing is ever pushed to waiters. The work runner's claim
    query correlates waiting items against this ledger, which makes event
    waiting crash-safe with the exact same machinery as time waiting.

    Namespaces:
    - "interface.*" and "work.*" are RUNTIME-OWNED (emitted by the bridge
      and the work runner respectively; the AI's signal.emit refuses them).
    - everything else is open for gated emission by intelligence.
    """

    __tablename__ = "runtime_signals"
    __table_args__ = (Index("ix_runtime_signals_name_time", "name", "emitted_at"),)

    # Signal name, e.g. "interface.message:<principal_id>" or
    # "work.succeeded:<work_id>". Format-validated by
    # wax.runtime.work.signals.validate_signal_name.
    name: Mapped[str] = mapped_column(String(160), nullable=False)

    # Optional JSON evidence about what happened. NEVER secrets.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    # When the fact happened (UTC). Emission is insert-only; RETENTION is
    # not append-only — the maintenance loop prunes old/excess rows with
    # the waiter-safety rule (ADR-0015).
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Attribution: "bridge", "work_runner", or "principal:<id>".
    emitted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
