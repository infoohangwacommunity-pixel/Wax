"""Role seeding and default role assignment.

The forensic audit (Section 7) found that principals created via WhatsApp
were assigned NO role, which — under the authorization system that already
existed — meant zero permissions for every real user, forever. The roles
table itself was only ever populated by tests.

This module makes the built-in roles real:
- seed_builtin_roles(): idempotently inserts BUILTIN_ROLES at startup.
- ensure_principal_role(): idempotently assigns a default role (member) to
  a principal on first contact, with an audit trail.

Assigning "member" is a policy decision encoded ONCE here, not scattered:
the member role grants memory.read/write, capability.invoke:built_in and
execution permissions — see authority/permissions.py.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.authority.permissions import BUILTIN_ROLES
from wax.observability.audit import record_audit_event
from wax.runtime.logging import get_logger
from wax.state.authority_models import PrincipalRole, Role

log = get_logger(__name__)

DEFAULT_ROLE_FOR_NEW_PRINCIPALS = "member"


async def seed_builtin_roles(session: AsyncSession) -> None:
    """Insert any missing built-in roles. Idempotent — safe on every boot."""
    for name, permissions in BUILTIN_ROLES.items():
        existing = (
            await session.execute(select(Role).where(Role.name == name))
        ).scalar_one_or_none()
        if existing is not None:
            # Keep stored permissions in sync with the declared source of truth.
            if set(existing.permissions or set()) != set(permissions):
                existing.permissions = sorted(permissions)
                await session.flush()
                log.info("authority.role.updated", role=name, permission_count=len(permissions))
            continue
        session.add(
            Role(
                id=str(ULID()),
                name=name,
                description=f"Built-in role: {name}",
                permissions=sorted(permissions),
            )
        )
        log.info("authority.role.seeded", role=name, permission_count=len(permissions))
    await session.flush()


async def get_role_by_name(session: AsyncSession, name: str) -> Role | None:
    return (await session.execute(select(Role).where(Role.name == name))).scalar_one_or_none()


async def ensure_principal_role(
    session: AsyncSession,
    principal_id: str,
    role_name: str = DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
    *,
    assigned_by: str | None = None,
) -> PrincipalRole | None:
    """Idempotently assign `role_name` to `principal_id`.

    Returns the assignment, or None if the role does not exist (honest
    degradation: no silent success — caller decides what to do).
    """
    role = await get_role_by_name(session, role_name)
    if role is None:
        log.error("authority.default_role_missing", role=role_name)
        return None

    existing = (
        await session.execute(
            select(PrincipalRole).where(
                PrincipalRole.principal_id == principal_id,
                PrincipalRole.role_id == role.id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    assignment = PrincipalRole(
        id=str(ULID()),
        principal_id=principal_id,
        role_id=role.id,
    )
    session.add(assignment)
    await session.flush()
    await record_audit_event(
        session,
        actor_principal_id=assigned_by,
        actor_kind="system",
        event_kind="authority.role.assigned",
        outcome="success",
        payload={"principal_id": principal_id, "role": role_name},
    )
    log.info(
        "authority.principal_role.assigned",
        principal_id=principal_id,
        role=role_name,
    )
    return assignment
