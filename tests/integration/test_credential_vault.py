"""Credential Vault — integration tests (ADR-0040, Phase 7).

Verifies the vault NEVER exposes secrets to the model:
- credential.connect returns connection_id (not the secret)
- credential.list returns metadata only (no secrets)
- credential.request returns opaque handle (not the secret)
- credential.revoke immediately invalidates
- secret storage is encrypted at rest (the DB row's secret_blob is
  not the plaintext)
- wrong principal cannot use a connection
- nonexistent connector is rejected
- insufficient scopes are rejected
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.runtime.services import RuntimeServices
from wax.runtime.vault import (
    decrypt_secret,
    encrypt_secret,
    seed_builtin_connectors,
)
from wax.state.credential_models import (
    CredentialEventRecord,
    PrincipalConnectionRecord,
)
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
        await s.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings):
    return RuntimeServices.build(test_settings)


async def _create_principal(*, display_name: str = "Test", phone: str = "1234567890") -> str:
    from wax.authority.seed import (
        DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
        ensure_principal_role,
    )

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name=display_name)
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
        await s.commit()
        return principal.id


# ----------------------------------------------------------------------------
# 1. Encryption at rest
# ----------------------------------------------------------------------------


class TestEncryptionAtRest:
    def test_encrypt_decrypt_roundtrip(self):
        original = "ghp_secrettoken_12345"
        encrypted = encrypt_secret(original)
        decrypted = decrypt_secret(encrypted)
        assert decrypted == original
        assert encrypted != original  # the encrypted form is NOT the plaintext

    def test_encrypt_does_not_leak_plaintext_prefix(self):
        secret = "UNIQUE_PREFIX_abc123"
        encrypted = encrypt_secret(secret)
        assert "UNIQUE_PREFIX" not in encrypted


# ----------------------------------------------------------------------------
# 2. credential.connect
# ----------------------------------------------------------------------------


class TestCredentialConnect:
    async def test_connect_returns_connection_id_not_secret(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "ghp_test_token_12345",
                        "scopes": ["repository.read", "repository.write"],
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert "connection_id" in result.outputs
        # The secret is NOT in the output
        assert "ghp_test_token_12345" not in str(result.outputs)

        # The DB row's secret_blob is NOT the plaintext
        async with db_session() as s:
            record = (
                await s.execute(
                    select(PrincipalConnectionRecord).where(
                        PrincipalConnectionRecord.principal_id == principal_id
                    )
                )
            ).scalar_one()
            assert record.secret_blob != "ghp_test_token_12345"
            assert "ghp_test_token_12345" not in record.secret_blob

    async def test_connect_rejects_unknown_connector(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "nonexistent_brand",
                        "secret": "some_token",
                        "scopes": ["x"],
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "Unknown connector" in (result.error or "")

    async def test_connect_rejects_unsupported_scope(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "some_token",
                        "scopes": ["nonexistent_scope"],
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "not supported" in (result.error or "").lower()

    async def test_connect_rotates_existing_connection(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            r1 = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "old_token",
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()
            r2 = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "new_token",
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()

        assert r1.outcome == "success"
        assert r2.outcome == "success"
        # Two connection records; the first is revoked
        async with db_session() as s:
            records = (
                (
                    await s.execute(
                        select(PrincipalConnectionRecord)
                        .where(PrincipalConnectionRecord.principal_id == principal_id)
                        .where(PrincipalConnectionRecord.connector_name == "git_host")
                    )
                )
                .scalars()
                .all()
            )
            assert len(records) == 2
            statuses = [r.status for r in records]
            assert "revoked" in statuses
            assert "active" in statuses


# ----------------------------------------------------------------------------
# 3. credential.list
# ----------------------------------------------------------------------------


class TestCredentialList:
    async def test_list_returns_metadata_only(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "ghp_secret_xyz",
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.list",
                    principal_id=principal_id,
                    inputs={},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["count"] >= 1
        # The secret is NOT in the output
        assert "ghp_secret_xyz" not in str(result.outputs)


# ----------------------------------------------------------------------------
# 4. credential.request
# ----------------------------------------------------------------------------


class TestCredentialRequest:
    async def test_request_returns_opaque_handle(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            connect_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "ghp_handle_test",
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()
            connection_id = connect_result.outputs["connection_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.request",
                    principal_id=principal_id,
                    inputs={
                        "connection_id": connection_id,
                        "scopes": ["repository.read"],
                        "purpose": "read source code",
                        "ttl_seconds": 3600,
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert "handle" in result.outputs
        assert "grant_id" in result.outputs
        # The secret is NOT in the output
        assert "ghp_handle_test" not in str(result.outputs)

    async def test_request_rejects_insufficient_scopes(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            connect_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "ghp_scope_test",
                        "scopes": ["repository.read"],  # only read
                    },
                )
            )
            await s.commit()
            connection_id = connect_result.outputs["connection_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.request",
                    principal_id=principal_id,
                    inputs={
                        "connection_id": connection_id,
                        "scopes": ["repository.write"],  # not granted
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "does not grant scopes" in (result.error or "")

    async def test_request_rejects_other_principal_connection(self, fresh_db, services):
        principal_a = await _create_principal(display_name="A", phone="1111111111")
        principal_b = await _create_principal(display_name="B", phone="2222222222")

        async with db_session() as s:
            invoker = services.invoker(s)
            connect_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_a,
                    inputs={
                        "connector": "git_host",
                        "secret": "ghp_owner_test",
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()
            connection_id = connect_result.outputs["connection_id"]

        # Principal B tries to request a grant on A's connection
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.request",
                    principal_id=principal_b,
                    inputs={
                        "connection_id": connection_id,
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "different principal" in (result.error or "")


# ----------------------------------------------------------------------------
# 5. credential.revoke
# ----------------------------------------------------------------------------


class TestCredentialRevoke:
    async def test_revoke_connection_immediate(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            connect_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "ghp_revoke_test",
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()
            connection_id = connect_result.outputs["connection_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.revoke",
                    principal_id=principal_id,
                    inputs={"connection_id": connection_id, "reason": "compromised"},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert any("connection:" in r for r in result.outputs["revoked"])

        # The connection is now revoked — further grant requests should fail
        async with db_session() as s:
            invoker = services.invoker(s)
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
        assert grant_result.outcome == "failure"
        assert "revoked" in (grant_result.error or "")

    async def test_revoke_grant_immediate(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            connect_result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "ghp_revoke_grant_test",
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
            grant_id = grant_result.outputs["grant_id"]

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.revoke",
                    principal_id=principal_id,
                    inputs={"grant_id": grant_id},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert any("grant:" in r for r in result.outputs["revoked"])


# ----------------------------------------------------------------------------
# 6. Audit events
# ----------------------------------------------------------------------------


class TestCredentialAuditEvents:
    async def test_connect_creates_audit_event(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="credential.connect",
                    principal_id=principal_id,
                    inputs={
                        "connector": "git_host",
                        "secret": "ghp_audit_test",
                        "scopes": ["repository.read"],
                    },
                )
            )
            await s.commit()

        async with db_session() as s:
            events = (
                (
                    await s.execute(
                        select(CredentialEventRecord).where(
                            CredentialEventRecord.principal_id == principal_id,
                            CredentialEventRecord.kind == "connected",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(events) >= 1
            assert events[0].connector_name == "git_host"
            # The event never contains the secret
            assert "ghp_audit_test" not in str(events)
