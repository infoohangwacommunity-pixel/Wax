"""Integration tests for Phase G (Capabilities).

Tests verify:
- Registry register / get / list / unregister
- Invoker authorizes before executing (INV-04)
- Invoker returns outcome=denied when authorization fails
- Invoker returns outcome=success when authorized
- Invoker returns outcome=timeout when capability exceeds timeout
- Invoker returns outcome=failure on implementation exception
- Invoker returns outcome=not_found for unknown capability
- Built-in echo and http.get work end-to-end
- AI principal (no permissions) is denied — INV-04
"""

from __future__ import annotations

import asyncio

import pytest

from wax.authority.service import AuthorizationService
from wax.capabilities.built_ins import register_builtins
from wax.capabilities.contracts import (
    CapabilityDescriptor,
    CapabilityInvocationRequest,
    CapabilityStatus,
)
from wax.capabilities.invoker import CapabilityInvoker
from wax.capabilities.registry import CapabilityRegistry
from wax.identity.repository import PrincipalRepository
from wax.state.authority_models import Role
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await dispose_engine()


@pytest.fixture
def registry() -> CapabilityRegistry:
    reg = CapabilityRegistry()
    register_builtins(reg)
    return reg


class TestRegistry:
    def test_register_and_get(self, registry: CapabilityRegistry) -> None:
        desc, impl = registry.get("echo")
        assert desc.name == "echo"
        assert callable(impl)

    def test_get_unknown_raises(self, registry: CapabilityRegistry) -> None:
        from wax.core.exceptions import WaxNotFoundError

        with pytest.raises(WaxNotFoundError):
            registry.get("does.not.exist")

    def test_list_includes_all_builtins(self, registry: CapabilityRegistry) -> None:
        names = {d.name for d in registry.list_capabilities()}
        assert "echo" in names
        assert "http.get" in names

    def test_register_duplicate_raises(self, registry: CapabilityRegistry) -> None:
        from wax.core.exceptions import WaxStateConflictError

        with pytest.raises(WaxStateConflictError):
            registry.register(
                CapabilityDescriptor(
                    name="echo",
                    description="duplicate",
                    required_permission="capability.invoke:built_in",
                ),
                echo_impl_replacement,
            )

    def test_unregister_removes(self, registry: CapabilityRegistry) -> None:
        registry.unregister("echo")
        assert "echo" not in registry

    def test_set_status_changes_status(self, registry: CapabilityRegistry) -> None:
        registry.set_status("echo", CapabilityStatus.DEGRADED)
        assert registry.get_status("echo") == CapabilityStatus.DEGRADED


async def echo_impl_replacement(inputs: dict, ctx: object = None) -> dict:
    return {"echo": inputs}


class TestInvokerAuthorization:
    """INV-04: AI-requested actions must pass through runtime authorization."""

    async def test_invoke_denied_for_principal_without_permission(
        self, fresh_db, registry: CapabilityRegistry
    ) -> None:
        """A principal without the required permission is denied."""
        async with db_session() as session:
            # Create a principal with no roles
            repo = PrincipalRepository(session)
            principal = await repo.create_principal()
            await session.commit()

        async with db_session() as session:
            auth = AuthorizationService(session)
            invoker = CapabilityInvoker(registry, auth)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="echo",
                    principal_id=principal.id,
                    inputs={"message": "hello"},
                )
            )
            await session.commit()

        assert result.outcome == "denied"
        assert "permission" in (result.error or "").lower()

    async def test_invoke_succeeds_for_authorized_principal(
        self, fresh_db, registry: CapabilityRegistry
    ) -> None:
        """A principal with the right permission can invoke."""
        async with db_session() as session:
            # Create role with built_in permission
            role = Role(
                id="01HXY" + "0" * 21,
                name="member",
                description="Member role",
            )
            role.add_permission("capability.invoke:built_in")
            session.add(role)

            repo = PrincipalRepository(session)
            principal = await repo.create_principal()

            auth = AuthorizationService(session)
            await auth.assign_role(principal.id, role.id)
            await session.commit()

        async with db_session() as session:
            auth = AuthorizationService(session)
            invoker = CapabilityInvoker(registry, auth)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="echo",
                    principal_id=principal.id,
                    inputs={"message": "hello world"},
                )
            )
            await session.commit()

        assert result.outcome == "success"
        assert result.outputs is not None
        assert result.outputs["echo"]["message"] == "hello world"

    async def test_invoke_not_found_for_unknown_capability(
        self, fresh_db, registry: CapabilityRegistry
    ) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.create_principal()
            await session.commit()

        async with db_session() as session:
            auth = AuthorizationService(session)
            invoker = CapabilityInvoker(registry, auth)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="does.not.exist",
                    principal_id=principal.id,
                    inputs={},
                )
            )
            await session.commit()

        assert result.outcome == "not_found"

    async def test_invoke_denied_for_unavailable_capability(
        self, fresh_db, registry: CapabilityRegistry
    ) -> None:
        """A capability marked unavailable cannot be invoked."""
        async with db_session() as session:
            role = Role(
                id="01HXY" + "2" * 21,
                name="member2",
            )
            role.add_permission("capability.invoke:built_in")
            session.add(role)

            repo = PrincipalRepository(session)
            principal = await repo.create_principal()

            auth = AuthorizationService(session)
            await auth.assign_role(principal.id, role.id)
            await session.commit()

        # Mark echo as unavailable
        registry.set_status("echo", CapabilityStatus.UNAVAILABLE)

        async with db_session() as session:
            auth = AuthorizationService(session)
            invoker = CapabilityInvoker(registry, auth)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="echo",
                    principal_id=principal.id,
                    inputs={"message": "hello"},
                )
            )
            await session.commit()

        assert result.outcome == "denied"
        assert "unavailable" in (result.error or "").lower()

    async def test_invoke_records_audit_trail(self, fresh_db, registry: CapabilityRegistry) -> None:
        """Every invocation must produce an audit record."""
        async with db_session() as session:
            role = Role(
                id="01HXY" + "3" * 21,
                name="audited_member",
            )
            role.add_permission("capability.invoke:built_in")
            session.add(role)

            repo = PrincipalRepository(session)
            principal = await repo.create_principal()

            auth = AuthorizationService(session)
            await auth.assign_role(principal.id, role.id)
            await session.commit()
            principal_id = principal.id

        async with db_session() as session:
            auth = AuthorizationService(session)
            invoker = CapabilityInvoker(registry, auth)
            await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="echo",
                    principal_id=principal_id,
                    inputs={"message": "audit test"},
                )
            )
            await session.commit()

        # Verify audit records exist
        async with db_session() as session:
            from sqlalchemy import select

            from wax.state.audit_models import AuditEvent

            result = await session.execute(
                select(AuditEvent)
                .where(AuditEvent.actor_principal_id == principal_id)
                .where(AuditEvent.event_kind == "auth_decision")
            )
            events = list(result.scalars().all())
            assert len(events) >= 1
            # The auth decision should record the capability name
            payloads = [e.payload for e in events if e.payload]
            capabilities = [p.get("capability") for p in payloads if p.get("capability")]
            assert "echo" in capabilities

    async def test_invoke_handles_capability_failure_gracefully(self, fresh_db) -> None:
        """If a capability implementation raises, the invoker returns failure."""
        # Register a capability that always raises
        registry = CapabilityRegistry()
        registry.register(
            CapabilityDescriptor(
                name="always.fails",
                description="Always raises for testing",
                required_permission="capability.invoke:built_in",
                timeout_seconds=5.0,
            ),
            _failing_impl,
        )

        async with db_session() as session:
            role = Role(
                id="01HXY" + "4" * 21,
                name="failer_member",
            )
            role.add_permission("capability.invoke:built_in")
            session.add(role)

            repo = PrincipalRepository(session)
            principal = await repo.create_principal()
            auth = AuthorizationService(session)
            await auth.assign_role(principal.id, role.id)
            await session.commit()

        async with db_session() as session:
            auth = AuthorizationService(session)
            invoker = CapabilityInvoker(registry, auth)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="always.fails",
                    principal_id=principal.id,
                    inputs={},
                )
            )
            await session.commit()

        assert result.outcome == "failure"
        assert "RuntimeError" in (result.error or "")

    async def test_invoke_enforces_timeout(self, fresh_db) -> None:
        """A capability that exceeds its timeout returns outcome=timeout."""
        registry = CapabilityRegistry()
        registry.register(
            CapabilityDescriptor(
                name="slow.capability",
                description="Always times out",
                required_permission="capability.invoke:built_in",
                timeout_seconds=0.1,
            ),
            _slow_impl,
        )

        async with db_session() as session:
            role = Role(
                id="01HXY" + "5" * 21,
                name="slow_member",
            )
            role.add_permission("capability.invoke:built_in")
            session.add(role)

            repo = PrincipalRepository(session)
            principal = await repo.create_principal()
            auth = AuthorizationService(session)
            await auth.assign_role(principal.id, role.id)
            await session.commit()

        async with db_session() as session:
            auth = AuthorizationService(session)
            invoker = CapabilityInvoker(registry, auth)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="slow.capability",
                    principal_id=principal.id,
                    inputs={},
                )
            )
            await session.commit()

        assert result.outcome == "timeout"


async def _failing_impl(inputs: dict, ctx: object = None) -> dict:
    raise RuntimeError("intentional failure")


async def _slow_impl(inputs: dict, ctx: object = None) -> dict:
    await asyncio.sleep(2.0)
    return {"result": "should never reach here"}
