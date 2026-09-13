"""Authorization service — the single point where "may this principal do X?" is answered.

INVARIANT INV-04: AI-requested actions must pass through runtime authorization.
This service is the enforcement point.

Architectural rules:
- The service never trusts caller-provided permission claims. It always
  queries the database for the principal's actual roles and their
  permissions.
- The service is the ONLY place where "may this principal do X?" is
  answered. No other code path may make authorization decisions.
- The service records every authorization decision to the audit log.
- The service never throws on denial — it returns False and the caller
  must convert that into the appropriate response (HTTP 403, etc.).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.authority.models import PrincipalRole, Role
from wax.authority.permissions import is_permission_granted
from wax.runtime.logging import get_logger
from wax.state.audit_models import AuditEvent
from ulid import ULID

log = get_logger(__name__)


class AuthorizationService:
    """The runtime's sole authority for "may this principal do X?".

    Usage:
        auth = AuthorizationService(session)
        if not await auth.check(principal_id, "memory.read"):
            raise WaxPermissionDeniedError(...)
        # authorized — proceed
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def check(
        self,
        principal_id: str | None,
        permission: str,
        *,
        actor_kind: str = "human",
        capability_name: str | None = None,
        request_id: str | None = None,
    ) -> bool:
        """Check whether `principal_id` may perform `permission`.

        Returns True if authorized, False otherwise. NEVER raises on denial.

        Records every check to the audit log (success or denial).
        """
        if principal_id is None:
            # Anonymous (system-initiated) requests have no principal. They
            # are denied by default — the caller must explicitly allow
            # system-initiated actions by skipping this check.
            await self._audit(
                actor_principal_id=None,
                actor_kind="system",
                event_kind="auth_decision",
                outcome="denied",
                payload={
                    "permission": permission,
                    "reason": "anonymous_request",
                    "capability": capability_name,
                },
                request_id=request_id,
            )
            return False

        granted = await self._get_permissions(principal_id)

        authorized = is_permission_granted(granted, permission)

        await self._audit(
            actor_principal_id=principal_id,
            actor_kind=actor_kind,
            event_kind="auth_decision",
            outcome="success" if authorized else "denied",
            payload={
                "permission": permission,
                "capability": capability_name,
                "granted_count": len(granted),
            },
            request_id=request_id,
        )

        if not authorized:
            log.warning(
                "authority.denied",
                principal_id=principal_id,
                permission=permission,
                capability=capability_name,
            )
        else:
            log.debug(
                "authority.granted",
                principal_id=principal_id,
                permission=permission,
                capability=capability_name,
            )

        return authorized

    async def _get_permissions(self, principal_id: str) -> frozenset[str]:
        """Return all permissions granted to `principal_id` via their roles."""
        from wax.state.authority_models import (
            PrincipalRole as PR,
            Role as R,
        )

        result = await self._session.execute(
            select(R.permissions)
            .join(PR, PR.role_id == R.id)
            .where(PR.principal_id == principal_id)
        )
        perms: set[str] = set()
        for row in result.scalars():
            # row is a list of permission strings
            if row:
                perms.update(row)
        return frozenset(perms)

    async def assign_role(
        self,
        principal_id: str,
        role_id: str,
        *,
        assigned_by: str | None = None,
        request_id: str | None = None,
    ) -> PrincipalRole:
        """Assign a role to a principal.

        Requires `authority.role.assign` permission on the caller.
        """
        pr = PrincipalRole(
            id=str(ULID()),
            principal_id=principal_id,
            role_id=role_id,
        )
        self._session.add(pr)
        await self._session.flush()

        await self._audit(
            actor_principal_id=assigned_by,
            actor_kind="human",
            event_kind="authority.role.assign",
            outcome="success",
            payload={
                "principal_id": principal_id,
                "role_id": role_id,
            },
            request_id=request_id,
        )
        return pr

    async def _audit(
        self,
        *,
        actor_principal_id: str | None,
        actor_kind: str,
        event_kind: str,
        outcome: str,
        payload: dict[str, object],
        request_id: str | None = None,
    ) -> None:
        """Record an audit event. Append-only."""
        event = AuditEvent(
            id=str(ULID()),
            actor_principal_id=actor_principal_id,
            actor_kind=actor_kind,
            event_kind=event_kind,
            outcome=outcome,
            payload=payload,
            request_id=request_id,
        )
        self._session.add(event)
        await self._session.flush()
