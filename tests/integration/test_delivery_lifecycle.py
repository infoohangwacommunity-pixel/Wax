"""Durable outbound delivery lifecycle (ADR-0021, mission §55).

"If WAX successfully completes work but cannot deliver the result:
that should become recoverable state." These tests prove the full arc
over the REAL ASGI app and the REAL webhook path:

- a completed reply whose interface send fails becomes a pending
  delivery record (not a graveyard row, not silence);
- the maintenance pass retries it through the delivery router and the
  record becomes delivered when the interface recovers;
- exhaustion is a terminal honest failure;
- the deliverability horizon expires stale pending records;
- a missing interface sender is a retryable condition, never a success.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from wax.state.delivery_models import DeliveryRecord
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
def services(test_settings):
    from wax.runtime.services import RuntimeServices

    return RuntimeServices.build(test_settings)

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "wax-test-verify-token"
PHONE_NUMBER_ID = "1234567890"
SENDER_PHONE = "2348012345678"


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(
        APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()


def _meta_text_payload(message_id: str, text: str) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": PHONE_NUMBER_ID,
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "2348000000000",
                                "phone_number_id": PHONE_NUMBER_ID,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Delivery"},
                                    "wa_id": SENDER_PHONE,
                                }
                            ],
                            "messages": [
                                {
                                    "from": SENDER_PHONE,
                                    "id": message_id,
                                    "timestamp": "1700000000",
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        }
                    }
                ],
            }
        ],
    }


def _failing_client() -> Any:
    """A WhatsApp client whose Graph API sends always fail (500)."""
    from wax.interfaces.whatsapp.client import WhatsAppClient

    client = WhatsAppClient(
        access_token="test-access-token",
        phone_number_id=PHONE_NUMBER_ID,
        app_secret=APP_SECRET,
        verify_token=VERIFY_TOKEN,
    )
    client._client = httpx.AsyncClient(
        base_url=WhatsAppClient.BASE_URL,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": "down"})
        ),
    )
    return client


class TestDeliveryLifecycle:
    async def test_failed_reply_becomes_pending_delivery_record(
        self, client: httpx.AsyncClient, app: Any, test_settings: Any
    ) -> None:
        app.state.whatsapp_client = _failing_client()
        payload = _meta_text_payload(f"wamid.DELIV{datetime.now(UTC).microsecond}", "Hello delivery")
        body = json.dumps(payload).encode()
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

        async with db_session() as session:
            from sqlalchemy import select

            from wax.state.identity_models import PrincipalCredential

            records = (
                (await session.execute(select(DeliveryRecord))).scalars().all()
            )
            credential = (
                (
                    await session.execute(
                        select(PrincipalCredential).where(
                            PrincipalCredential.value == SENDER_PHONE
                        )
                    )
                )
                .scalars()
                .one()
            )

        assert len(records) == 1, (
            "the failed reply must be recoverable state, not silence"
        )
        record = records[0]
        assert record.status == "pending"
        assert record.attempts == 1
        assert record.source == "bridge_reply"
        assert record.interface_kind == "whatsapp"
        assert record.recipient_id == SENDER_PHONE
        assert record.principal_id == credential.principal_id
        assert "500" in (record.last_error or "") or "HTTP" in (record.last_error or "")
        assert record.text  # the reply content is preserved verbatim
        assert record.next_attempt_at is not None  # backoff scheduled

    async def test_maintenance_retry_delivers_when_interface_recovers(
        self, client: httpx.AsyncClient, app: Any, test_settings: Any
    ) -> None:
        from wax.runtime.maintenance import run_maintenance_pass

        app.state.whatsapp_client = _failing_client()
        payload = _meta_text_payload(
            f"wamid.RETRY{datetime.now(UTC).microsecond}", "Hello retry"
        )
        body = json.dumps(payload).encode()
        await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)},
        )

        # The interface recovers: a working sender replaces the failing one.
        delivered_log: list[tuple[str, str]] = []

        async def _working_sender(recipient: str, text: str) -> dict:
            delivered_log.append((recipient, text))
            return {"message_id": "fake-ack"}

        app.state.services.delivery.register("whatsapp", _working_sender)

        # The backoff has not elapsed — the sweep must NOT burn an attempt.
        early = await run_maintenance_pass(test_settings, services=app.state.services)
        assert early["delivery_retries"]["due"] == 0

        async with db_session() as session:
            from sqlalchemy import select

            record = (
                (await session.execute(select(DeliveryRecord))).scalars().one()
            )
            record.next_attempt_at = None  # backoff elapses (test clock)
            await session.commit()

        results = await run_maintenance_pass(
            test_settings, services=app.state.services
        )
        assert results["delivery_retries"]["due"] == 1
        assert results["delivery_retries"]["delivered"] == 1

        async with db_session() as session:
            from sqlalchemy import select

            record = (
                (await session.execute(select(DeliveryRecord))).scalars().one()
            )
        assert record.status == "delivered"
        assert record.delivered_at is not None
        assert delivered_log and delivered_log[0][1] == record.text

    async def test_exhaustion_is_terminal_honest_failure(
        self, fresh_db, services
    ) -> None:
        """Attempts exhausted → failed, with the last error preserved."""
        from wax.runtime.delivery_queue import DeliveryQueue

        async def _never_works(recipient: str, text: str) -> dict:
            raise RuntimeError("interface down")

        services.delivery.register("whatsapp", _never_works)

        async with db_session() as session:
            queue = DeliveryQueue(
                session, services, retry_backoff_seconds=0.0, max_age_seconds=86400.0
            )
            record = await queue.enqueue(
                principal_id="01_testprincipal000000000",
                interface_kind="whatsapp",
                recipient_id="+2348000000001",
                text="will never arrive",
                source="capability",
                max_attempts=3,
            )
            for _ in range(3):
                await queue.attempt(record)
            await session.commit()

            assert record.status == "failed"
            assert record.attempts == 3
            assert "interface down" in (record.last_error or "")
            # A terminal record is not re-attempted.
            assert await queue.attempt(record) is False
            assert record.attempts == 3

    async def test_deliverability_horizon_expires_stale_pending(
        self, fresh_db, services
    ) -> None:
        from wax.runtime.delivery_queue import DeliveryQueue

        async def _works(recipient: str, text: str) -> dict:
            return {"message_id": "ack"}

        services.delivery.register("whatsapp", _works)

        async with db_session() as session:
            queue = DeliveryQueue(
                session, services, retry_backoff_seconds=1.0, max_age_seconds=3600.0
            )
            record = await queue.enqueue(
                principal_id="01_testprincipal000000000",
                interface_kind="whatsapp",
                recipient_id="+2348000000001",
                text="too late",
                source="capability",
            )
            # The record was created long ago (test clock).
            record.created_at = datetime.now(UTC) - timedelta(seconds=7200)
            await session.commit()

            delivered = await queue.attempt(record)
            await session.commit()

        assert delivered is False
        assert record.status == "failed"
        assert "deliverability horizon" in (record.last_error or "")

    async def test_missing_sender_is_retryable_never_success(
        self, fresh_db, services
    ) -> None:
        from wax.runtime.delivery_queue import DeliveryQueue

        # A router with NO whatsapp sender attached (adapter down).
        async with db_session() as session:
            queue = DeliveryQueue(
                session, services, retry_backoff_seconds=10.0, max_age_seconds=86400.0
            )
            record = await queue.enqueue(
                principal_id="01_testprincipal000000000",
                interface_kind="whatsapp",
                recipient_id="+2348000000001",
                text="no interface yet",
                source="capability",
                max_attempts=5,
            )
            delivered = await queue.attempt(record)
            await session.commit()

        assert delivered is False
        assert record.status == "pending"  # retrying, not failed, not sent
        assert "no delivery interface attached" in (record.last_error or "")
