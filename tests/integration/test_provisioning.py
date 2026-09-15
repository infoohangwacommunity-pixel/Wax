"""Dynamic provisioning tests (Phase S).

Audit Table 7, objective F ("organize my project") stopped at: "No files,
no external services (Phase S unbuilt)". These tests prove the runtime can
now provision ephemeral resources with owner, lifecycle, limits, cleanup,
and audit — as a mechanism, with no use case hardcoded.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponseStatus,
)
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.provisioning import (
    ProvisioningError,
    ProvisioningService,
)
from wax.runtime.services import RuntimeServices
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.provisioning_models import ProvisionedResourceRecord


@pytest.fixture
async def fresh_db(test_settings, tmp_path):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    test_settings.__dict__["provisioning_root"] = str(tmp_path / "resources")
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        await seed_builtin_roles(session)
        await session.commit()
    yield test_settings
    await dispose_engine()


@pytest.fixture
def services(test_settings) -> RuntimeServices:
    return RuntimeServices.build(test_settings)


@pytest.fixture
def provisioning(test_settings) -> ProvisioningService:
    return ProvisioningService(test_settings)


@pytest.fixture
def principal_id() -> str:
    from ulid import ULID

    return str(ULID())


def _request(message_id: str = "msg-prov-1") -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id="+2348000000000",
        sender_display_name="Test User",
        text="I need a workspace",
        received_at=datetime.now(UTC),
    )


class TestProvisioningService:
    async def test_provision_creates_dir_row_and_audit(
        self, fresh_db, provisioning, principal_id
    ) -> None:
        from wax.state.audit_models import AuditEvent

        async with db_session() as session:
            record = await provisioning.provision_scratch_dir(
                session, principal_id=principal_id, ttl_seconds=600
            )
            await session.commit()

        assert record.status == "active"
        from pathlib import Path

        assert Path(record.uri).is_dir()
        assert record.expires_at is not None
        assert record.expires_at > datetime.now(UTC)

        async with db_session() as session:
            event = (
                await session.execute(
                    select(AuditEvent).where(AuditEvent.event_kind == "provisioning.allocated")
                )
            ).scalar_one()
        assert event.actor_principal_id == principal_id

    async def test_ttl_limits_are_enforced(self, fresh_db, provisioning, principal_id):
        with pytest.raises(ProvisioningError, match="24-hour"):
            async with db_session() as session:
                await provisioning.provision_scratch_dir(
                    session, principal_id=principal_id, ttl_seconds=86400 * 7
                )
        with pytest.raises(ProvisioningError, match="positive"):
            async with db_session() as session:
                await provisioning.provision_scratch_dir(
                    session, principal_id=principal_id, ttl_seconds=0
                )

    async def test_active_resource_limit_per_principal(
        self, fresh_db, test_settings, provisioning, principal_id
    ) -> None:
        limit = test_settings.provisioning_max_active_per_principal  # default 5
        async with db_session() as session:
            for _ in range(limit):
                await provisioning.provision_scratch_dir(
                    session, principal_id=principal_id, ttl_seconds=600
                )
            await session.commit()
            with pytest.raises(ProvisioningError, match="active resources"):
                await provisioning.provision_scratch_dir(
                    session, principal_id=principal_id, ttl_seconds=600
                )

    async def test_ttl_reaper_destroys_directory_and_marks_expired(
        self, fresh_db, provisioning, principal_id
    ) -> None:
        from pathlib import Path

        async with db_session() as session:
            record = await provisioning.provision_scratch_dir(
                session, principal_id=principal_id, ttl_seconds=600
            )
            # Force expiry.
            record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            uri = record.uri

        assert Path(uri).is_dir()
        async with db_session() as session:
            expired = await provisioning.expire_due(session)
            await session.commit()
        assert expired == 1
        assert not Path(uri).exists()

        async with db_session() as session:
            row = await session.get(ProvisionedResourceRecord, record.id)
        assert row.status == "expired"  # the audit row survives

    async def test_release_is_owner_only(self, fresh_db, provisioning) -> None:
        from ulid import ULID

        owner = str(ULID())
        stranger = str(ULID())
        async with db_session() as session:
            record = await provisioning.provision_scratch_dir(
                session, principal_id=owner, ttl_seconds=600
            )
            await session.commit()

        with pytest.raises(ProvisioningError, match="different principal"):
            async with db_session() as session:
                await provisioning.release(session, resource_id=record.id, principal_id=stranger)

        async with db_session() as session:
            released = await provisioning.release(
                session, resource_id=record.id, principal_id=owner
            )
            await session.commit()
        assert released is True

    async def test_promotion_clears_ttl(self, fresh_db, provisioning, principal_id):
        async with db_session() as session:
            record = await provisioning.provision_scratch_dir(
                session, principal_id=principal_id, ttl_seconds=600
            )
            promoted = await provisioning.promote(
                session, resource_id=record.id, principal_id=principal_id
            )
            await session.commit()
        assert promoted is True
        async with db_session() as session:
            row = await session.get(ProvisionedResourceRecord, record.id)
        assert row.status == "active"
        assert row.expires_at is None  # intentionally permanent now

        # The reaper must NOT destroy promoted resources.
        async with db_session() as session:
            expired = await provisioning.expire_due(session)
        assert expired == 0


class TestScratchWorkspaceCapability:
    async def test_end_to_end_via_bridge_and_invoker(
        self, fresh_db, services, test_settings
    ) -> None:
        """User message → AI requests scratch.workspace → a real ephemeral
        directory exists with a TTL — no filesystem feature was built, the
        provisioning MECHANISM was."""
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
        assert response.status == RuntimeResponseStatus.SUCCESS

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="scratch.workspace",
                    principal_id=response.principal_id,
                    inputs={"ttl_seconds": 600},
                )
            )
            await session.commit()
        assert result.outcome == "success", result.error

        # P0-12: path field removed from scratch.workspace output
        # The test now checks the resource_id instead
        assert result.outputs["resource_id"]
        assert result.outputs["kind"] == "scratch_dir"

    async def test_invalid_ttl_is_honestly_refused(self, fresh_db, services) -> None:
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="scratch.workspace",
                    principal_id=response.principal_id,
                    inputs={"ttl_seconds": 10_000_000},
                )
            )
        assert result.outcome == "failure"
        # The invoker now enforces the DECLARED contract (audit fix):
        # ttl_seconds above the descriptor's maximum is refused at the
        # boundary with the bound named, before the implementation runs.
        assert "86400" in result.error
