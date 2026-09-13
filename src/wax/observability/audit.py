"""Audit event writer for runtime components.

The forensic audit (Sections 17, 20, 31) found that audit_events existed as
a table and the authority/agency services could write it, but NO production
path ever wrote a row: after real message processing the probe counted zero
audit events. This module gives every runtime component a single, explicit
way to append audit events.

Append-only by convention. Never store secrets or model chain-of-thought.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.state.audit_models import AuditEvent


async def record_audit_event(
    session: AsyncSession,
    *,
    actor_principal_id: str | None,
    actor_kind: str,  # "human" | "ai" | "system"
    event_kind: str,
    outcome: str,  # "success" | "denied" | "pending" | "failed" | ...
    payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Append an audit event and flush. Never raises on payload issues —
    the caller is expected to pass serializable payloads."""
    event = AuditEvent(
        id=str(ULID()),
        actor_principal_id=actor_principal_id,
        actor_kind=actor_kind,
        event_kind=event_kind,
        outcome=outcome,
        payload=payload or {},
        request_id=request_id,
    )
    session.add(event)
    await session.flush()
