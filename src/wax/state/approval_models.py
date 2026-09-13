"""Persistence for the human-approval authority boundary.

When the agency gate decides an AI-requested action needs explicit human
authorization, the runtime does not simply deny — it creates a PENDING
APPROVAL: a durable, auditable, expiring, one-time-use authorization
request bound to the human principal on whose behalf the AI acts.

The trust model (this is the core of the primitive):
- The AI can CREATE nothing here directly — pending approvals are created
  by the runtime's agency gate, never by model output.
- The AI can never DECIDE: the decision API authenticates a HUMAN
  principal (resolved from an interface credential, the same identity
  path as inbound messages). There is no "approve" capability.
- One approval authorizes exactly ONE action: the request fingerprint
  (capability + canonical inputs) must match, and consumption marks the
  row consumed (replay protection).
- Approvals expire: a pending request past its deadline is honestly
  dead (swept by the runtime, never silently ignored).

Nothing here knows about payments, emails, study plans, or any domain —
this is pure authority infrastructure.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin

# Lifecycle: pending → approved | denied ; pending → expired | cancelled.
# An approved approval that has been used by exactly one action becomes
# consumed (consumed_at set) while staying approved for audit.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_DENIED = "denied"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
VALID_STATUSES = frozenset(
    {STATUS_PENDING, STATUS_APPROVED, STATUS_DENIED, STATUS_EXPIRED, STATUS_CANCELLED}
)


class PendingApprovalRecord(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """A pending human-authorization request (the approval primitive)."""

    __tablename__ = "pending_approvals"
    __table_args__ = (
        Index("ix_pending_approvals_principal_status", "principal_id", "status"),
        Index("ix_pending_approvals_fingerprint_status", "request_fingerprint", "status"),
        Index("ix_pending_approvals_status_expires", "status", "expires_at"),
    )

    # The HUMAN this approval belongs to (the authority whose decision is
    # required). The AI acts on their behalf and can never decide here.
    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)

    # The execution that requested the action (traceability).
    requested_by_execution_id: Mapped[str | None] = mapped_column(
        String(26), nullable=True
    )

    # What action is being authorized — generic fields only:
    capability_name: Mapped[str] = mapped_column(String(128), nullable=False)
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # Safe summary of the requested inputs (keys + truncated values,
    # secrets never enter this table). Evidence for the human's decision.
    scope_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Fingerprint of (principal, capability, canonical inputs). Two
    # properties: the same request re-issued while pending returns the
    # SAME row (idempotency); an approved row authorizes only the exact
    # same request (no scope creep via mutated inputs).
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_PENDING)

    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Decision provenance (who decided, when, optional human note).
    decided_by: Mapped[str | None] = mapped_column(String(26), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Replay protection: consumption marks one-time use, bound to the
    # execution that consumed it.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_by_execution_id: Mapped[str | None] = mapped_column(String(26), nullable=True)
