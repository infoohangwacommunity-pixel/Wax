"""Generic Connector Runtime — integration tests (ADR-0041, Phase 8).

Verifies the runtime understands resource types, NOT brands:
- connector.discover lists all universal connectors
- connector.discover with a filter returns only matching connectors
- connector.resolve returns a binding handle + service kind
- connector.resolve rejects invalid grant handle
- connector.resolve rejects wrong principal
- the intelligence never sees raw secrets
"""

from __future__ import annotations

import pytest

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.runtime.services import RuntimeServices
from wax.runtime.vault import seed_builtin_connectors
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as s:
        await seed_builtin_roles(s)
        await seed_builtin_connectors(s)
        # P0-Taxonomy: seed a test connector definition since the core
        # no longer hardcodes connector types
        from ulid import ULID

        from wax.state.credential_models import ConnectorDefinitionRecord

        s.add(
            ConnectorDefinitionRecord(
                id=str(ULID()),
                name="git_host",
                description="Git hosting service (discovered)",
                supported_scopes=["repository.read", "repository.write", "repository.admin"],
                auth_methods=["api_key", "bearer_token"],
                version="1.0.0",
            )
        )
        s.add(
            ConnectorDefinitionRecord(
                id=str(ULID()),
                name="package_registry",
                description="Package registry (discovered)",
                supported_scopes=["package.read", "package.publish"],
                auth_methods=["api_key", "bearer_token"],
                version="1.0.0",
            )
        )
        await s.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings):
    return RuntimeServices.build(test_settings)


async def _create_principal(*, display_name: str = "Test", phone: str = "1234567890") -> str:
    from wax.authority.seed import ensure_principal_role

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name=display_name)
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, "admin")
        await s.commit()
        return principal.id


async def _connect_and_request_grant(
    services, principal_id: str, connector: str = "git_host"
) -> str:
    """Helper: connect a credential + request a grant. Returns the handle."""
    async with db_session() as s:
        invoker = services.invoker(s)
        connect_result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name="credential.connect",
                principal_id=principal_id,
                inputs={
                    "connector": connector,
                    "secret": f"test_secret_{connector}",
                    "scopes": ["repository.read"] if connector == "git_host" else ["package.read"],
                },
            )
        )
        await s.commit()
        connection_id = connect_result.outputs["connection_id"]

        grant_result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name="credential.request",
                principal_id=principal_id,
                inputs={
                    "connection_id": connection_id,
                    "scopes": ["repository.read"] if connector == "git_host" else ["package.read"],
                },
            )
        )
        await s.commit()
        return grant_result.outputs["handle"]


# ----------------------------------------------------------------------------
# 1. connector.discover
# ----------------------------------------------------------------------------


class TestConnectorDiscover:
    async def test_discover_lists_all_universal_connectors(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="connector.discover",
                    principal_id=principal_id,
                    inputs={},
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert result.outputs["count"] >= 2  # only seeded connectors (git_host, package_registry)
        names = [c["name"] for c in result.outputs["connectors"]]
        assert "git_host" in names
        assert "package_registry" in names
        # Only git_host and package_registry are seeded in tests (core no longer hardcodes)

    async def test_discover_with_filter(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="connector.discover",
                    principal_id=principal_id,
                    inputs={"connector": "git_host"},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["count"] == 1
        assert result.outputs["connectors"][0]["name"] == "git_host"
        # The scopes are declared
        assert "repository.read" in result.outputs["connectors"][0]["supported_scopes"]

    async def test_discover_with_nonexistent_filter_returns_empty(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="connector.discover",
                    principal_id=principal_id,
                    inputs={"connector": "nonexistent_brand"},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["count"] == 0

    async def test_discover_never_returns_secrets(self, fresh_db, services):
        """The discovery output is metadata only — no secret_blob column."""
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="connector.discover",
                    principal_id=principal_id,
                    inputs={},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        # The output should not contain any 'secret' field
        assert "secret" not in str(result.outputs).lower()


# ----------------------------------------------------------------------------
# 2. connector.resolve
# ----------------------------------------------------------------------------


class TestConnectorResolve:
    async def test_resolve_returns_binding_handle_and_service_kind(self, fresh_db, services):
        principal_id = await _create_principal()
        handle = await _connect_and_request_grant(services, principal_id, "git_host")

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="connector.resolve",
                    principal_id=principal_id,
                    inputs={"grant_handle": handle},
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert "binding_handle" in result.outputs
        assert result.outputs["service_kind"] == "unknown"  # core does not hardcode brands
        assert result.outputs["connector"] == "git_host"
        assert result.outputs["available_operations"] == []  # core does not hardcode operations

    async def test_resolve_rejects_invalid_handle(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="connector.resolve",
                    principal_id=principal_id,
                    inputs={"grant_handle": "nonexistent_handle"},
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "Invalid grant handle" in (result.error or "")

    async def test_resolve_rejects_wrong_principal(self, fresh_db, services):
        principal_a = await _create_principal(display_name="A", phone="1111111111")
        principal_b = await _create_principal(display_name="B", phone="2222222222")
        handle = await _connect_and_request_grant(services, principal_a, "git_host")

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="connector.resolve",
                    principal_id=principal_b,  # different principal
                    inputs={"grant_handle": handle},
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "different principal" in (result.error or "")

    async def test_resolve_never_exposes_secret(self, fresh_db, services):
        principal_id = await _create_principal()
        # Connect with a recognizable secret prefix
        async with db_session() as s:
            invoker = services.invoker(s)
            connect_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "UNIQUE_SECRET_MARKER_abc123",
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()
            connection_id = connect_result.outputs["connection_id"]

            grant_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.request",
                    principal_id=principal_id,
                    inputs={
                        "connection_id": connection_id,
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()
            handle = grant_result.outputs["handle"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="connector.resolve",
                    principal_id=principal_id,
                    inputs={"grant_handle": handle},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        # The secret is NEVER in the resolve output
        assert "UNIQUE_SECRET_MARKER_abc123" not in str(result.outputs)

    async def test_resolve_for_package_registry(self, fresh_db, services):
        principal_id = await _create_principal()
        handle = await _connect_and_request_grant(services, principal_id, "package_registry")

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="connector.resolve",
                    principal_id=principal_id,
                    inputs={"grant_handle": handle},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["service_kind"] == "unknown"
        assert result.outputs["connector"] == "package_registry"
        assert result.outputs["available_operations"] == []  # core does not hardcode operations
