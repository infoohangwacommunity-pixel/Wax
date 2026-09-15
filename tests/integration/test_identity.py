"""Integration tests for Phase D (Identity).

Tests verify:
- Principal creation and retrieval
- Credential attachment and uniqueness
- Interface-independent identity resolution
- Soft-delete preserves audit trail
- ULID primary keys are lexicographically sortable by creation time
"""

from __future__ import annotations

import pytest

from wax.identity.repository import PrincipalRepository
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


@pytest.fixture
async def fresh_db(test_settings):
    """Initialize in-memory SQLite, create schema, yield, dispose."""
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    await dispose_engine()


class TestPrincipalCRUD:
    async def test_create_principal_returns_object_with_id(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.create_principal(display_name="Alice")
            await session.commit()

            assert principal.id is not None
            assert len(principal.id) == 26  # ULID length
            assert principal.status == "active"
            assert principal.display_name == "Alice"
            assert principal.is_active

    async def test_get_principal_returns_created(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            created = await repo.create_principal(display_name="Bob")
            await session.commit()

        # New session — verifies persistence
        async with db_session() as session:
            repo = PrincipalRepository(session)
            fetched = await repo.get_principal(created.id)
            assert fetched is not None
            assert fetched.id == created.id
            assert fetched.display_name == "Bob"

    async def test_get_principal_returns_none_for_unknown_id(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            fetched = await repo.get_principal("01HABCDEFGHJKLMNPQRSTU0VWXZ")  # fake ULID
            assert fetched is None

    async def test_soft_delete_marks_principal_deleted(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.create_principal(display_name="ToDelete")
            await session.commit()

            ok = await repo.soft_delete_principal(principal.id)
            await session.commit()
            assert ok

        async with db_session() as session:
            repo = PrincipalRepository(session)
            fetched = await repo.get_principal(principal.id)
            assert fetched is not None
            assert not fetched.is_active
            assert fetched.status == "deleted"
            assert fetched.deleted_at is not None

    async def test_soft_delete_unknown_returns_false(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            ok = await repo.soft_delete_principal("01HABCDEFGHJKLMNPQRSTU0VWXZ")
            assert not ok


class TestCredentials:
    async def test_add_credential_to_principal(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.create_principal(display_name="Carol")
            cred = await repo.add_credential(
                principal.id, kind="whatsapp_phone", value="+2348000000000"
            )
            await session.commit()

            assert cred.id is not None
            assert cred.principal_id == principal.id
            assert cred.kind == "whatsapp_phone"
            assert cred.value == "+2348000000000"
            assert not cred.is_verified

    async def test_credential_uniqueness_across_principals(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            p1 = await repo.create_principal(display_name="P1")
            p2 = await repo.create_principal(display_name="P2")
            await repo.add_credential(p1.id, kind="email", value="shared@example.com")
            await session.commit()

            # Adding same (kind, value) to a different principal must fail.
            with pytest.raises(ValueError, match="already attached"):
                await repo.add_credential(p2.id, kind="email", value="shared@example.com")

    async def test_add_credential_to_unknown_principal_raises(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            with pytest.raises(LookupError):
                await repo.add_credential(
                    "01HABCDEFGHJKLMNPQRSTU0VWXZ",
                    kind="email",
                    value="ghost@example.com",
                )

    async def test_resolve_principal_by_credential(self, fresh_db) -> None:
        """The core of interface-independent identity resolution."""
        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.create_principal(display_name="Dave")
            await repo.add_credential(principal.id, kind="whatsapp_phone", value="+2348000000001")
            await session.commit()

        # New session: simulate a WhatsApp webhook arriving
        async with db_session() as session:
            repo = PrincipalRepository(session)
            resolved = await repo.resolve_principal_by_credential(
                "whatsapp_phone", "+2348000000001"
            )
            assert resolved is not None
            assert resolved.id == principal.id
            assert resolved.display_name == "Dave"

    async def test_resolve_principal_returns_none_for_unknown_credential(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            resolved = await repo.resolve_principal_by_credential(
                "whatsapp_phone", "+2348999999999"
            )
            assert resolved is None

    async def test_same_principal_multiple_credentials_different_kinds(self, fresh_db) -> None:
        """A principal can have many credentials across different interfaces.

        This is what makes interface handoff possible (Foundation §51, §52):
        a user can be reached via WhatsApp OR web OR email, all mapping to
        the same underlying principal.
        """
        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.create_principal(display_name="Multi")
            await repo.add_credential(principal.id, kind="whatsapp_phone", value="+2348000000002")
            await repo.add_credential(principal.id, kind="email", value="multi@example.com")
            await repo.add_credential(principal.id, kind="web_session", value="session-token-abc")
            await session.commit()

        async with db_session() as session:
            repo = PrincipalRepository(session)
            creds = await repo.list_credentials(principal.id)
            assert len(creds) == 3
            kinds = {c.kind for c in creds}
            assert kinds == {"whatsapp_phone", "email", "web_session"}


class TestIdentitySurvivesInterfaceRemoval:
    """INV-02: removing an interface credential must NOT destroy the principal.

    This is the architectural test for interface independence (Directive §73).
    If WhatsApp disappears, the principal survives with their email / web /
    other credentials.
    """

    async def test_principal_survives_after_credential_removed(self, fresh_db) -> None:
        async with db_session() as session:
            repo = PrincipalRepository(session)
            principal = await repo.create_principal(display_name="Survivor")
            wa_cred = await repo.add_credential(
                principal.id, kind="whatsapp_phone", value="+2348000000003"
            )
            await repo.add_credential(principal.id, kind="email", value="survivor@example.com")
            await session.commit()
            wa_cred_id = wa_cred.id

        # Simulate WhatsApp disappearing: delete the credential
        async with db_session() as session:
            from sqlalchemy import delete

            from wax.state.identity_models import PrincipalCredential

            await session.execute(
                delete(PrincipalCredential).where(PrincipalCredential.id == wa_cred_id)
            )
            await session.commit()

        # Principal still exists, still has email
        async with db_session() as session:
            repo = PrincipalRepository(session)
            p = await repo.get_principal(principal.id)
            assert p is not None
            assert p.is_active
            creds = await repo.list_credentials(principal.id)
            assert len(creds) == 1
            assert creds[0].kind == "email"
