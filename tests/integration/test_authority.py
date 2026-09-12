"""Integration tests for Phase E (Authority / Authorization).

Tests verify:
- Permission checking works for exact matches, wildcards, namespace wildcards
- Role assignment grants permissions to a principal
- Audit log records every authorization decision
- Anonymous requests are denied by default
- The AI principal has NO permissions of its own (INV-04)
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.authority.permissions import (
    BUILTIN_PERMISSIONS,
    BUILTIN_ROLES,
    is_permission_granted,
)
from wax.authority.service import AuthorizationService
from wax.core.config import settings_for_testing
from wax.identity.repository import PrincipalRepository
from wax.state.audit_models import AuditEvent
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.authority_models import Role


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


class TestPermissionMatching:
    """Pure-function tests for is_permission_granted."""

    def test_exact_match_grants(self) -> None:
        assert is_permission_granted(frozenset({"memory.read"}), "memory.read")

    def test_no_match_denies(self) -> None:
        assert not is_permission_granted(frozenset({"memory.write"}), "memory.read")

    def test_capability_any_matches_specific(self) -> None:
        assert is_permission_granted(
            frozenset({"capability.invoke:any"}),
            "capability.invoke:web_search",
        )

    def test_capability_any_does_not_match_other_namespace(self) -> None:
        assert not is_permission_granted(
            frozenset({"capability.invoke:any"}),
            "memory.read",
        )

    def test_namespace_wildcard_matches_subpermissions(self) -> None:
        assert is_permission_granted(frozenset({"admin.*"}), "admin.role.assign")
        assert is_permission_granted(frozenset({"admin.*"}), "admin.anything.else")

    def test_namespace_wildcard_does_not_match_other_namespace(self) -> None:
        assert not is_permission_granted(frozenset({"admin.*"}), "memory.read")


class TestAuthorizationService:
    """Tests for the runtime-enforced authorization service."""

    async def test_check_returns_false_for_anonymous(self, fresh_db) -> None:
        """Anonymous requests (principal_id=None) are always denied."""
        async with db_session() as session:
            auth = AuthorizationService(session)
            ok = await auth.check(None, "memory.read", actor_kind="system")
            assert not ok

    async def test_check_returns_false_for_principal_without_role(
        self, fresh_db
    ) -> None:
        """A principal with no roles has no permissions."""
        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.create_principal()
            await session.commit()

            auth = AuthorizationService(session)
            ok = await auth.check(principal.id, "memory.read")
            assert not ok

    async def test_check_returns_true_when_role_grants_permission(
        self, fresh_db
    ) -> None:
        """A principal with a role that grants the permission is authorized."""
        async with db_session() as session:
            # Create role with permission
            role = Role(
                id="01HXY" + "0" * 21,  # fake ULID
                name="test_member",
                description="Test role",
            )
            role.permissions.append("memory.read")
            role.permissions.append("memory.write")
            session.add(role)
            await session.flush()

            # Create principal
            repo = PrincipalRepository(session)
            principal = await repo.create_principal()
            await session.flush()

            # Assign role to principal
            auth = AuthorizationService(session)
            await auth.assign_role(principal.id, role.id, assigned_by=None)
            await session.commit()

            ok = await auth.check(principal.id, "memory.read")
            assert ok

    async def test_audit_recorded_for_every_decision(self, fresh_db) -> None:
        """Every check() must write an audit record (INV-06)."""
        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.create_principal()
            await session.commit()

            auth = AuthorizationService(session)
            # Denied (no role)
            await auth.check(principal.id, "memory.read")
            await session.commit()

        async with db_session() as session:
            result = await session.execute(
                select(AuditEvent).where(
                    AuditEvent.actor_principal_id == principal.id
                )
            )
            events = list(result.scalars().all())
            assert len(events) >= 1
            assert events[0].event_kind == "auth_decision"
            assert events[0].outcome == "denied"
            assert events[0].payload["permission"] == "memory.read"

    async def test_audit_recorded_for_anonymous_too(self, fresh_db) -> None:
        """Even anonymous requests generate audit records."""
        async with db_session() as session:
            auth = AuthorizationService(session)
            await auth.check(None, "memory.read", actor_kind="system")
            await session.commit()

        async with db_session() as session:
            result = await session.execute(
                select(AuditEvent).where(AuditEvent.actor_principal_id.is_(None))
            )
            events = list(result.scalars().all())
            assert len(events) >= 1
            assert events[0].actor_kind == "system"

    async def test_ai_principal_has_no_permissions(self, fresh_db) -> None:
        """INV-04: the AI is an untrusted requester with no inherent permissions.

        The 'ai' role is intentionally empty. The model cannot grant itself
        authority by producing text.
        """
        async with db_session() as session:
            # Create the ai role with empty permissions
            role = Role(
                id="01HXY" + "1" * 21,
                name="ai",
                description="The AI itself — intentionally has NO permissions.",
            )
            # No permissions added — empty
            session.add(role)
            await session.flush()

            # Create principal and assign ai role
            repo = PrincipalRepository(session)
            principal = await repo.create_principal()
            auth = AuthorizationService(session)
            await auth.assign_role(principal.id, role.id)
            await session.commit()

            # Try every built-in permission — all must be denied
            for permission in BUILTIN_PERMISSIONS:
                if permission == "admin.*":
                    continue  # wildcard, not a real permission
                ok = await auth.check(
                    principal.id, permission, actor_kind="ai"
                )
                assert not ok, (
                    f"AI principal was granted {permission!r} — violates INV-04"
                )


class TestBuiltinRoles:
    """Verify the declared built-in roles are sensible."""

    def test_admin_has_all_permissions(self) -> None:
        admin_perms = BUILTIN_ROLES["admin"]
        # Admin should have memory, capability, execution, identity, authority
        assert "memory.read" in admin_perms
        assert "memory.write" in admin_perms
        assert "capability.invoke:any" in admin_perms
        assert "execution.start" in admin_perms

    def test_ai_role_is_empty(self) -> None:
        """INV-04: the AI has no inherent permissions."""
        assert len(BUILTIN_ROLES["ai"]) == 0

    def test_member_has_basic_capabilities(self) -> None:
        member = BUILTIN_ROLES["member"]
        assert "memory.read" in member
        assert "memory.write" in member
        assert "capability.invoke:built_in" in member
        # Member should NOT have admin or authority permissions
        assert "authority.role.assign" not in member
        assert "admin.*" not in member

    def test_service_role_can_read_audit(self) -> None:
        service = BUILTIN_ROLES["service"]
        assert "audit.read" in service
