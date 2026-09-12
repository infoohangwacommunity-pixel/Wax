"""wax.state.audit_models — audit log persistence.

Every security-sensitive action must be attributable (INV-06). The audit
log is the canonical record of "who did what, when, under what authority."

Architectural rules:
- Audit logs are append-only. UPDATE and DELETE are forbidden on this table.
- The `actor_principal_id` may be NULL only for system-initiated actions.
- The `actor_kind` discriminator records whether the actor was a human,
  a service, or the AI itself (the AI is always an untrusted requester).
- The `payload` JSONB column captures context (capability name, inputs
  metadata, authorization decision, etc.) — but NEVER captures secrets
  or model chain-of-thought (INV-09 of observability).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from wax.state.models import Base, TimestampMixin, ULIDPrimaryKeyMixin


class AuditEvent(Base, ULIDPrimaryKeyMixin, TimestampMixin):
    """An immutable record of a security-sensitive action.

    Append-only. The application layer enforces this; a future architecture
    test will verify no UPDATE/DELETE statement targets this table.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_principal", "actor_principal_id"),
        Index("ix_audit_events_kind", "event_kind"),
        Index("ix_audit_events_created_at", "created_at"),
    )

    # Who initiated the action.
    # NULL = system-initiated (cron, internal cleanup, etc.)
    actor_principal_id: Mapped[str | None] = mapped_column(
        String(26), nullable=True, index=True
    )

    # What kind of actor: human | service | ai | system
    actor_kind: Mapped[str] = mapped_column(String(32), nullable=False)

    # What kind of event: auth_decision, capability_invoke, identity_create,
    # identity_delete, secret_rotate, etc.
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)

    # Outcome: success | denied | failure | error
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)

    # Structured payload (capability name, inputs metadata, authz decision,
    # resource id, error message, etc.). NEVER contains secrets.
    # Use SQLAlchemy's generic JSON type — on PostgreSQL this maps to JSONB,
    # on SQLite it maps to TEXT with JSON serialization.
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True
    )

    # Optional request_id / trace_id for cross-referencing logs.
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
