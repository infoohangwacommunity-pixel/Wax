"""Reliability regression tests (spec §17, tests A-K).

These tests prove the production reliability properties demanded by the
Wax Production Reliability Remediation directive:

  Test A — normal message: accepted → durable work → executed → delivered
  Test B — duplicate webhook: identical message twice → one execution
  Test C — concurrent duplicate: two simultaneous → one accepted, one dup
  Test D — no cost limiter: no RATE_LIMITED/cost_exceeded from a dollar budget
  Test E — memory consolidation: with wired intelligence → no NoneType.complete
  Test F — memory failure isolation: consolidation failure doesn't kill conversation
  Test G — provider timeout: one timeout → retry → success
  Test H — provider permanent failure: all attempts fail → durable failure state
  Test I — outbound WhatsApp failure: response persisted + delivery queued + retry
  Test J — worker restart: start work, terminate worker, restart → work recovered
  Test K — webhook speed: webhook returns fast even when LLM is slow
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponseStatus,
)
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.inbound import accept_inbound_message
from wax.runtime.services import RuntimeServices
from wax.state.bridge_models import ProcessedMessageRecord
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.work_models import WorkItemRecord

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings: Any, tmp_path):
    """Use a file-based SQLite so concurrent connections share the same DB.
    In-memory SQLite (`:memory:`) is per-connection, which breaks
    concurrent-duplicate testing."""
    db_file = tmp_path / "test_reliability.db"
    test_settings.__dict__["database_url"] = f"sqlite+aiosqlite:///{db_file}"
    test_settings.__dict__["terminal_working_dir_root"] = "/tmp/wax-test-workspaces"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_settings
    await dispose_engine()


@pytest.fixture
def services(test_settings: Any):
    mock = MockLLMProvider()
    intel = IntelligenceService(mock)
    return RuntimeServices.build(test_settings, intelligence=intel)


@pytest.fixture
def bridge(services: Any):
    return RuntimeBridge(intelligence=services.require_intelligence(), services=services)


def _request(
    *,
    message_id: str = "msg-1",
    text: str = "Hello WAX",
    phone: str = "+1234567890",
):
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id=phone,
        sender_display_name="Test User",
        text=text,
        received_at=datetime.now(UTC),
    )


# ===========================================================================
# Test A — normal message: accepted → durable work → executed → delivered
# ===========================================================================


class TestANormalMessage:
    """A normal message is accepted, durable work is created, and the
    webhook returns immediately without running intelligence."""

    async def test_accept_creates_durable_work(self, fresh_db, services, bridge) -> None:
        async with db_session() as session:
            result = await accept_inbound_message(
                session,
                interface_kind="whatsapp",
                interface_message_id="wamid.test.A.001",
                sender_interface_id="+2349138153604",
                sender_display_name="Alice",
                text="Hello WAX",
                received_at=datetime.now(UTC),
            )
            await session.commit()

        assert result.accepted is True
        assert result.principal_id is not None
        assert result.processed_message_id is not None

        # A durable work item was created
        async with db_session() as session:
            work_items = (
                (
                    await session.execute(
                        select(WorkItemRecord).where(
                            WorkItemRecord.principal_id == result.principal_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(work_items) == 1
        assert work_items[0].kind == "intelligence"
        assert work_items[0].status in ("pending", "running")

    async def test_processed_message_recorded(self, fresh_db, services, bridge) -> None:
        async with db_session() as session:
            await accept_inbound_message(
                session,
                interface_kind="whatsapp",
                interface_message_id="wamid.test.A.002",
                sender_interface_id="+2349138153605",
                sender_display_name="Test User",
                text="Hello",
                received_at=datetime.now(UTC),
            )
            await session.commit()

        async with db_session() as session:
            pm = (
                await session.execute(
                    select(ProcessedMessageRecord).where(
                        ProcessedMessageRecord.interface_message_id == "wamid.test.A.002"
                    )
                )
            ).scalar_one()
        assert pm.outcome == "pending"
        assert pm.principal_id is not None


# ===========================================================================
# Test B — duplicate webhook: identical message twice → one execution
# ===========================================================================


class TestBDuplicateWebhook:
    """The same WhatsApp message ID arrives twice (Meta retry). The
    second delivery must be acknowledged as a duplicate, NOT produce
    a second work item or a UniqueViolationError."""

    async def test_duplicate_acknowledged_no_second_work(self, fresh_db, services, bridge) -> None:
        msg_id = "wamid.test.B.001"
        async with db_session() as session:
            r1 = await accept_inbound_message(
                session,
                interface_kind="whatsapp",
                interface_message_id=msg_id,
                sender_interface_id="+2349138153606",
                sender_display_name="Test User",
                text="Hello",
                received_at=datetime.now(UTC),
            )
            await session.commit()

        async with db_session() as session:
            r2 = await accept_inbound_message(
                session,
                interface_kind="whatsapp",
                interface_message_id=msg_id,
                sender_interface_id="+2349138153606",
                sender_display_name="Test User",
                text="Hello",
                received_at=datetime.now(UTC),
            )
            await session.commit()

        assert r1.accepted is True
        assert r2.accepted is False  # duplicate
        assert r2.processed_message_id == r1.processed_message_id

        # Exactly one work item, not two
        async with db_session() as session:
            work_items = (
                (
                    await session.execute(
                        select(WorkItemRecord).where(WorkItemRecord.principal_id == r1.principal_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(work_items) == 1


# ===========================================================================
# Test C — concurrent duplicate: two simultaneous → one accepted, one dup
# ===========================================================================


class TestCConcurrentDuplicate:
    """Two webhook deliveries of the same message ID arrive concurrently.
    The database's unique constraint + ON CONFLICT DO NOTHING arbitrates
    exactly one winner. No UniqueViolationError ever reaches the app."""

    async def test_concurrent_deliveries_one_wins(self, fresh_db, services, bridge) -> None:
        msg_id = "wamid.test.C.001"

        # Fire two accept_inbound_message calls concurrently
        async def _accept():
            async with db_session() as session:
                result = await accept_inbound_message(
                    session,
                    interface_kind="whatsapp",
                    interface_message_id=msg_id,
                    sender_interface_id="+2349138153607",
                    sender_display_name="Test User",
                    text="Hello",
                    received_at=datetime.now(UTC),
                )
                await session.commit()
                return result

        r1, r2 = await asyncio.gather(_accept(), _accept())

        # Exactly one accepted, the other is a duplicate
        accepted_count = sum(1 for r in (r1, r2) if r.accepted)
        duplicate_count = sum(1 for r in (r1, r2) if not r.accepted)
        assert accepted_count == 1
        assert duplicate_count == 1

        # Exactly one work item, not two
        async with db_session() as session:
            principal_id = r1.principal_id if r1.accepted else r2.principal_id
            work_items = (
                (
                    await session.execute(
                        select(WorkItemRecord).where(WorkItemRecord.principal_id == principal_id)
                    )
                )
                .scalars()
                .all()
            )
        assert len(work_items) == 1


# ===========================================================================
# Test D — no cost limiter blocks a legitimate conversation
# ===========================================================================


class TestDNoCostLimiter:
    """Wax has NO internal dollar budget. A normal conversation must
    never return RATE_LIMITED or cost_exceeded because of a cost limit."""

    async def test_no_cost_protector_attribute(self, fresh_db, services, bridge) -> None:
        # The bridge MUST NOT have a _cost_protector attribute
        assert not hasattr(bridge, "_cost_protector"), (
            "RuntimeBridge still has _cost_protector — cost-accounting lifecycle "
            "was not fully removed"
        )

    async def test_no_cost_exceeded_log(self, fresh_db, services, bridge) -> None:
        # Process many messages — none should be rejected for cost
        statuses = []
        for i in range(20):
            async with db_session() as session:
                r = await bridge.process(
                    session,
                    _request(message_id=f"msg-cost-{i}", text=f"Message {i}"),
                )
                await session.commit()
                statuses.append(r.status)

        # All should succeed (or be rate-limited by infrastructure rate limit,
        # but NEVER by cost). The rate_limit_messages_per_hour default is 30,
        # so 20 messages should all succeed.
        for s in statuses:
            assert s == RuntimeResponseStatus.SUCCESS, (
                f"Message rejected with status={s} — cost limiting may still be active"
            )

    async def test_no_daily_cost_budget_in_config(self, fresh_db, services) -> None:
        # The config MUST NOT have daily_cost_budget_cents
        assert not hasattr(services.settings, "daily_cost_budget_cents"), (
            "WaxSettings still has daily_cost_budget_cents — cost config was not removed"
        )


# ===========================================================================
# Test E — memory consolidation with wired intelligence → no NoneType.complete
# ===========================================================================


class TestEMemoryConsolidation:
    """Memory consolidation with a correctly wired intelligence service
    must NOT crash with 'NoneType' object has no attribute 'complete'."""

    async def test_consolidation_does_not_crash_with_wired_intelligence(
        self, fresh_db, services, bridge
    ) -> None:
        # Create enough episodic memories to trigger consolidation

        from wax.identity.normalize import normalize_phone
        from wax.identity.repository import PrincipalRepository
        from wax.memory.contracts import MemoryCreate, MemoryKind
        from wax.memory.repository import MemoryRepository

        async with db_session() as session:
            repo = MemoryRepository(session)
            # Create a principal via the repository (handles ULID + credential)
            principal = await PrincipalRepository(session).create_principal(
                display_name="Test Consolidation"
            )
            await PrincipalRepository(session).add_credential(
                principal.id,
                kind="whatsapp_phone",
                value=normalize_phone("+2349138153608"),
                is_verified=True,
            )
            await session.flush()

            for i in range(5):
                await repo.create(
                    MemoryCreate(
                        principal_id=principal.id,
                        kind=MemoryKind.EPISODIC,
                        content={"text": f"User asked about chemistry topic {i}"},
                        provenance="test",
                        confidence=0.8,
                        importance=0.7,
                        observed_at=datetime.now(UTC),
                    )
                )
            await session.commit()

        # Run consolidation via maintenance — this MUST NOT raise
        # 'NoneType' object has no attribute 'complete'
        from wax.runtime.maintenance import _run_consolidation

        # This should succeed because services.intelligence is wired
        count = await _run_consolidation(services)
        # count may be 0 (consolidation may decide no clusters form a pattern)
        # — the point is it didn't crash
        assert isinstance(count, int)

    async def test_consolidation_skipped_gracefully_without_intelligence(
        self, fresh_db, test_settings
    ) -> None:
        """When intelligence is NOT wired (unit test scenario), consolidation
        logs + skips rather than crashing with NoneType.complete."""
        services_no_intel = RuntimeServices.build(test_settings)  # no intelligence
        assert services_no_intel.intelligence is None

        from wax.runtime.maintenance import _run_consolidation

        # Must NOT raise — must return 0 (skipped)
        count = await _run_consolidation(services_no_intel)
        assert count == 0


# ===========================================================================
# Test F — memory failure isolation: consolidation failure doesn't kill conversation
# ===========================================================================


class TestFMemoryFailureIsolation:
    """If memory consolidation fails, the active conversation must
    still succeed. Consolidation is a non-blocking maintenance task."""

    async def test_conversation_succeeds_when_consolidation_fails(
        self, fresh_db, services, bridge
    ) -> None:
        # Process a normal message — should succeed
        async with db_session() as session:
            r = await bridge.process(session, _request(message_id="msg-F-1", text="Hello"))
            await session.commit()
        assert r.status == RuntimeResponseStatus.SUCCESS

        # Force consolidation to fail by passing a broken intelligence
        # (the maintenance pass catches the exception and logs it,
        # doesn't re-raise). We simulate this by calling _run_consolidation
        # with a services object whose intelligence raises on .complete().
        class BrokenIntelligence:
            async def complete(self, request):
                raise RuntimeError("simulated provider failure")

        services.intelligence = BrokenIntelligence()
        from wax.runtime.maintenance import _run_consolidation

        # Consolidation fails per-principal but doesn't raise
        count = await _run_consolidation(services)
        assert isinstance(count, int)  # didn't crash

        # The conversation is still usable — process another message
        services.intelligence = IntelligenceService(MockLLMProvider())
        async with db_session() as session:
            r2 = await bridge.process(
                session, _request(message_id="msg-F-2", text="Still working?")
            )
            await session.commit()
        assert r2.status == RuntimeResponseStatus.SUCCESS


# ===========================================================================
# Test G — provider timeout → retry → success
# ===========================================================================


class TestGProviderTimeout:
    """A provider timeout on the first call should be retried by the
    ResilientProvider and ultimately succeed."""

    async def test_retry_after_timeout(self, fresh_db, test_settings) -> None:
        from wax.intelligence.contracts import (
            LLMRequest,
            LLMResponse,
            MessageRole,
            ProviderKind,
        )
        from wax.intelligence.resilience import ResilientProvider, TransientLLMError

        call_count = {"n": 0}

        class TimeoutThenSucceed:
            kind = ProviderKind.MOCK

            async def complete(self, request):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    raise TransientLLMError("simulated timeout")
                return LLMResponse(
                    content="recovered",
                    finish_reason="stop",
                    usage={},
                    model="test",
                    provider=ProviderKind.MOCK,
                )

            async def stream(self, request):
                raise NotImplementedError

            async def close(self):
                pass

        from wax.reliability.retry import RetryConfig

        resilient = ResilientProvider(
            TimeoutThenSucceed(),
            retry=RetryConfig(max_attempts=3, base_delay=0.01),
        )

        response = await resilient.complete(
            LLMRequest(messages=[(MessageRole.USER, "hi")])  # type: ignore[arg-type]
        )
        assert call_count["n"] == 2  # first failed, second succeeded
        assert response.content == "recovered"


# ===========================================================================
# Test H — provider permanent failure → durable failure state
# ===========================================================================


class TestHProviderPermanentFailure:
    """When all provider attempts fail permanently, the execution
    enters a durable failure state. The inbound message is NOT lost
    (it's in processed_messages), and a retry is scheduled."""

    async def test_permanent_failure_schedules_retry(self, fresh_db, services, bridge) -> None:
        # Replace the intelligence with one that always fails
        class AlwaysFails:
            async def complete(self, request):
                raise RuntimeError("permanent provider failure")

            async def stream(self, request):
                raise NotImplementedError

            async def close(self):
                pass

        bridge._intelligence = AlwaysFails()

        async with db_session() as session:
            r = await bridge.process(session, _request(message_id="msg-H-1", text="Hello"))
            await session.commit()

        # The bridge returns INTERNAL_ERROR (not SUCCESS)
        assert r.status == RuntimeResponseStatus.INTERNAL_ERROR

        # A retry work item was scheduled (durable)
        async with db_session() as session:
            work_items = (
                (
                    await session.execute(
                        select(WorkItemRecord).where(WorkItemRecord.kind == "intelligence")
                    )
                )
                .scalars()
                .all()
            )
        assert len(work_items) >= 1
        assert work_items[0].status in ("pending", "running")

        # The processed_message is recorded (NOT lost)
        async with db_session() as session:
            pm = (
                await session.execute(
                    select(ProcessedMessageRecord).where(
                        ProcessedMessageRecord.interface_message_id == "msg-H-1"
                    )
                )
            ).scalar_one()
        assert pm.outcome in ("internal_error", "failed")


# ===========================================================================
# Test I — outbound WhatsApp failure → response persisted + delivery queued
# ===========================================================================


class TestIOutboundFailure:
    """If WhatsApp send fails, the response is persisted as a DeliveryRecord
    with retry scheduled. The reply is NOT lost."""

    async def test_delivery_queued_on_send_failure(self, fresh_db, services, bridge) -> None:
        # Process a message successfully first
        async with db_session() as session:
            r = await bridge.process(session, _request(message_id="msg-I-1", text="Hello"))
            await session.commit()
        assert r.status == RuntimeResponseStatus.SUCCESS
        assert r.text is not None

        # Enqueue a delivery record (simulating what process_inbound does)
        from wax.runtime.delivery_queue import DeliveryQueue

        async with db_session() as session:
            queue = DeliveryQueue(
                session,
                services,
                retry_backoff_seconds=60.0,
                max_age_seconds=86400.0,
            )
            record = await queue.enqueue(
                principal_id=r.principal_id,
                interface_kind="whatsapp",
                recipient_id="+1234567890",
                text=r.text,
                source="bridge_reply",
                execution_id=r.execution_id,
                max_attempts=5,
            )
            # Attempt delivery — simulates WhatsApp being down
            # (no delivery interface registered → "no delivery interface attached")
            await queue.attempt(record)
            await session.commit()

            # The record is pending (retry scheduled), NOT lost
            assert record.status == "pending"
            assert record.attempts == 1
            assert record.next_attempt_at is not None
            assert "no delivery interface" in (record.last_error or "")


# ===========================================================================
# Test J — worker restart recovery
# ===========================================================================


class TestJWorkerRestart:
    """Start durable work, simulate worker restart, verify work is recovered."""

    async def test_pending_work_survives_session_close(self, fresh_db, services, bridge) -> None:
        # Accept an inbound message (creates durable work)
        async with db_session() as session:
            result = await accept_inbound_message(
                session,
                interface_kind="whatsapp",
                interface_message_id="wamid.test.J.001",
                sender_interface_id="+2349138153609",
                sender_display_name="Test User",
                text="Hello",
                received_at=datetime.now(UTC),
            )
            await session.commit()

        # Simulate "worker restart" by disposing + reinitializing the engine
        await dispose_engine()
        init_engine(fresh_db)

        # The work item is still in the DB — a new worker would pick it up
        async with db_session() as session:
            work_items = (
                (
                    await session.execute(
                        select(WorkItemRecord).where(
                            WorkItemRecord.principal_id == result.principal_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(work_items) == 1
        assert work_items[0].status in ("pending", "running")


# ===========================================================================
# Test K — webhook speed: webhook returns fast even when LLM is slow
# ===========================================================================


class TestKWebhookSpeed:
    """The webhook must return quickly even if the LLM is slow. The
    webhook does NOT call the LLM — it just durably accepts the message."""

    async def test_webhook_does_not_wait_for_llm(self, fresh_db, services, bridge) -> None:
        import time

        # Make the mock LLM slow (simulating a 10-second provider call)
        class SlowLLM:
            async def complete(self, request):
                await asyncio.sleep(10)  # simulating slow provider
                from wax.intelligence.contracts import LLMResponse

                return LLMResponse(
                    content="slow response", finish_reason="stop", usage={}, model="test"
                )

            async def stream(self, request):
                raise NotImplementedError

            async def close(self):
                pass

        # Replace the bridge's intelligence with the slow one
        bridge._intelligence = SlowLLM()

        # Time the webhook acceptance (NOT the bridge.process)
        start = time.monotonic()
        async with db_session() as session:
            result = await accept_inbound_message(
                session,
                interface_kind="whatsapp",
                interface_message_id="wamid.test.K.001",
                sender_interface_id="+2349138153610",
                sender_display_name="Test User",
                text="Hello",
                received_at=datetime.now(UTC),
            )
            await session.commit()
        elapsed = time.monotonic() - start

        assert result.accepted is True
        # The webhook must return in under 1 second — NOT wait for the 10s LLM call
        assert elapsed < 1.0, (
            f"Webhook took {elapsed:.2f}s — should be <1s. The webhook is "
            "waiting for the LLM, which violates the durable-ACK architecture."
        )
