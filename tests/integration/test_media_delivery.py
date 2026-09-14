"""Media + Delivery — integration tests (ADR-0043, Phase 10).

Covers:
- delivery.status returns delivery metadata (never message text)
- delivery.status by execution_id returns all deliveries for that execution
- delivery.retry resets a failed delivery to pending
- delivery.retry on an exhausted delivery is rejected
- delivery metadata never includes message text
"""

from __future__ import annotations

import pytest

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.runtime.services import RuntimeServices
from wax.state.delivery_models import DeliveryRecord
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


async def _create_delivery(
    principal_id: str,
    *,
    status: str = "pending",
    attempts: int = 0,
    max_attempts: int = 5,
    text: str = "SECRET_MESSAGE_CONTENT_xyz",
    execution_id: str | None = None,
) -> str:
    """Helper: create a delivery record directly."""
    from ulid import ULID

    async with db_session() as s:
        record = DeliveryRecord(
            id=str(ULID()),
            principal_id=principal_id,
            interface_kind="whatsapp",
            recipient_id="1234567890",
            text=text,
            status=status,
            attempts=attempts,
            max_attempts=max_attempts,
            source="bridge_reply",
            execution_id=execution_id,
        )
        s.add(record)
        await s.commit()
        return record.id


class TestDeliveryStatus:
    async def test_status_returns_metadata_only(self, fresh_db, services):
        principal_id = await _create_principal()
        delivery_id = await _create_delivery(principal_id, text="SECRET_content_xyz")

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="delivery.status",
                    principal_id=principal_id,
                    inputs={"delivery_id": delivery_id},
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert result.outputs["count"] == 1
        delivery = result.outputs["deliveries"][0]
        assert delivery["delivery_id"] == delivery_id
        assert delivery["status"] == "pending"
        # The message text is NEVER in the output
        assert "SECRET_content_xyz" not in str(result.outputs)

    async def test_status_by_execution_id(self, fresh_db, services):
        principal_id = await _create_principal()
        exec_id = "01TESTEXECID00000000000A"
        await _create_delivery(principal_id, execution_id=exec_id, text="msg1")
        await _create_delivery(principal_id, execution_id=exec_id, text="msg2")

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="delivery.status",
                    principal_id=principal_id,
                    inputs={"execution_id": exec_id},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["count"] == 2

    async def test_status_rejects_missing_params(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="delivery.status",
                    principal_id=principal_id,
                    inputs={},
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "either delivery_id or execution_id" in (result.error or "")


class TestDeliveryRetry:
    async def test_retry_resets_failed_to_pending(self, fresh_db, services):
        principal_id = await _create_principal()
        delivery_id = await _create_delivery(
            principal_id, status="failed", attempts=2, max_attempts=5
        )

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="delivery.retry",
                    principal_id=principal_id,
                    inputs={"delivery_id": delivery_id},
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        assert result.outputs["retried"] is True
        assert result.outputs["status"] == "pending"

        async with db_session() as s:
            record = await s.get(DeliveryRecord, delivery_id)
            assert record.status == "pending"

    async def test_retry_rejects_exhausted(self, fresh_db, services):
        principal_id = await _create_principal()
        delivery_id = await _create_delivery(
            principal_id, status="failed", attempts=5, max_attempts=5
        )

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="delivery.retry",
                    principal_id=principal_id,
                    inputs={"delivery_id": delivery_id},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["retried"] is False
        assert "max_attempts exhausted" in (result.outputs.get("reason") or "")

    async def test_retry_rejects_already_delivered(self, fresh_db, services):
        principal_id = await _create_principal()
        delivery_id = await _create_delivery(
            principal_id, status="delivered", attempts=1, max_attempts=5
        )

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="delivery.retry",
                    principal_id=principal_id,
                    inputs={"delivery_id": delivery_id},
                )
            )
            await s.commit()

        assert result.outcome == "success"
        assert result.outputs["retried"] is False
        assert "already delivered" in (result.outputs.get("reason") or "")

    async def test_retry_rejects_wrong_principal(self, fresh_db, services):
        principal_a = await _create_principal(display_name="A", phone="1111111111")
        principal_b = await _create_principal(display_name="B", phone="2222222222")
        delivery_id = await _create_delivery(principal_a)

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="delivery.retry",
                    principal_id=principal_b,
                    inputs={"delivery_id": delivery_id},
                )
            )
            await s.commit()

        assert result.outcome == "failure"
        assert "different principal" in (result.error or "")
