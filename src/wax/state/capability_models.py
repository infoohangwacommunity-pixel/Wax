"""ORM model — capability-invocation idempotency ledger (CV-19).

A capability invocation carrying an `idempotency_key` is claimed in the
database BEFORE the implementation runs. The claim row is the runtime's
memory of the request:

- `executing`  — a claim is held; identical requests are duplicates
                 until the claim is completed or its lease expires.
- `succeeded`  — the recorded response is returned on replay, so a
                 replay cannot double-execute an effect.
- `failed`     — the attempt failed; an identical retry may take over
                 the row and re-execute (at-least-once, the same honest
                 semantics the durable-work queue documents).

The ledger NEVER deletes rows: replay evidence is audit evidence. Rows
are bounded by (principal_id, capability_name, idempotency_key) — the
unique constraint makes double-claiming a DATABASE property, not a
convention.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from wax.state.models import Base, TimestampMixin


class CapabilityInvocationRecord(Base, TimestampMixin):
    """One claimed capability invocation, keyed by idempotency key."""

    __tablename__ = "capability_invocations"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    principal_id: Mapped[str] = mapped_column(String(26), nullable=False)
    capability_name: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # executing | succeeded | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # JSON-serialized outputs recorded on success (replay evidence).
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Bounded error text recorded on failure.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # While `executing`, the claim lease. An expired lease means the
    # claiming process died after claiming (effect state unknown); a
    # later identical request may take the claim over.
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # At most ONE ledger row per (principal, capability, key) — ever.
        Index(
            "uq_capability_invocations_principal_capability_key",
            "principal_id",
            "capability_name",
            "idempotency_key",
            unique=True,
        ),
        Index(
            "ix_capability_invocations_claim_expiry",
            "status",
            "claim_expires_at",
        ),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<CapabilityInvocationRecord {self.capability_name} "
            f"key={self.idempotency_key[:12]}… status={self.status}>"
        )
