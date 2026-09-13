"""Durable work runtime tests (Phase R / Phase V).

The founder's test case from the audit (Section 22): "Can WAX act without a
new user message?" The audited answer was NO — "no scheduling primitive
exists anywhere in the source; WAX cannot initiate a message without a
preceding inbound HTTP request."

These tests prove the mechanism now exists — as a RUNTIME MECHANISM, not a
feature. There is no ReminderService, no TimerService. A reminder is the
composition: work.schedule → capability handler → message.send.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityDescriptor
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import ToolCall
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponseStatus,
)
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.runtime.work import WorkRepository, WorkRunner, capability_handler
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.work_models import WorkItemRecord


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as session:
        await seed_builtin_roles(session)
        await session.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings) -> RuntimeServices:
    return RuntimeServices.build(test_settings)


@pytest.fixture
def runner(services: RuntimeServices) -> WorkRunner:
    wr = WorkRunner(
        services,
        poll_interval_seconds=0.05,
        lease_seconds=120.0,
        retry_backoff_seconds=0.0,
        stale_execution_seconds=900.0,
    )
    wr.register_handler("capability", capability_handler)
    return wr


def _request(
    message_id: str = "msg-work-1",
    text: str = "hello",
    sender_id: str = "+2348000000000",
) -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id=sender_id,
        sender_display_name="Test User",
        text=text,
        received_at=datetime.now(UTC),
    )


async def _get_work(work_id: str) -> WorkItemRecord:
    async with db_session() as session:
        item = await WorkRepository(session).get(work_id)
        # Detach fully: access every attribute we need inside the session.
        if item is not None:
            _ = (item.id, item.status, item.attempts, item.last_error, item.result)
            session.expunge(item)
        return item  # type: ignore[return-value]


class TestWorkScheduling:
    async def test_schedule_and_wake_executes_capability(self, fresh_db, services, runner) -> None:
        """work.schedule with delay_seconds → runner wakes → echo runs."""
        from wax.capabilities.contracts import CapabilityInvocationRequest

        # A principal with no roles cannot schedule (authority denies).
        async with db_session() as session:
            invoker = services.invoker(session)
            anonymous = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.schedule",
                    principal_id="01NO ROLES NO ROLES NO ROLES00".replace(" ", ""),
                    inputs={
                        "payload": {"capability_name": "echo", "inputs": {}},
                        "delay_seconds": 0.1,
                    },
                )
            )
            assert anonymous.outcome == "denied"

        # Real path: schedule through the invoker as a member principal.
        # First, create a principal through the bridge.
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
        principal_id = response.principal_id

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.schedule",
                    principal_id=principal_id,
                    inputs={
                        "payload": {
                            "capability_name": "echo",
                            "inputs": {"message": "wake up"},
                        },
                        "delay_seconds": 0.1,
                    },
                )
            )
            await session.commit()
        assert result.outcome == "success", result.error
        work_id = result.outputs["work_id"]

        item = await _get_work(work_id)
        assert item.status == "pending"

        # Before the wake time: nothing runs.
        ran = await runner.run_once()
        assert ran == 0

        await asyncio.sleep(0.15)
        ran = await runner.run_once()
        assert ran == 1

        item = await _get_work(work_id)
        assert item.status == "succeeded"
        assert item.result == {"echo": {"message": "wake up"}}

    async def test_schedule_unknown_capability_is_refused(self, fresh_db, services) -> None:
        """No fake success: scheduling work for a nonexistent capability
        fails at schedule time with a truthful error."""
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())

        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.schedule",
                    principal_id=response.principal_id,
                    inputs={
                        "payload": {"capability_name": "no.such.thing", "inputs": {}},
                        "delay_seconds": 60,
                    },
                )
            )
        assert result.outcome == "failure"
        assert "Unknown capability" in result.error

    async def test_cancel_own_work_and_ownership_boundary(self, fresh_db, services) -> None:
        from wax.capabilities.contracts import CapabilityInvocationRequest

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
        principal_id = response.principal_id

        async with db_session() as session:
            invoker = services.invoker(session)
            scheduled = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.schedule",
                    principal_id=principal_id,
                    inputs={
                        "payload": {"capability_name": "echo", "inputs": {}},
                        "delay_seconds": 3600,
                    },
                )
            )
            await session.commit()
        work_id = scheduled.outputs["work_id"]

        # Another (real, role-bearing) principal cannot cancel it.
        bridge_foreign = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            foreign_response = await bridge_foreign.process(
                session,
                _request(
                    message_id="msg-foreign-1",
                    text="hi from someone else",
                    sender_id="+2348111111111",
                ),
            )
            # Different sender → different principal.
        assert foreign_response.principal_id != principal_id

        async with db_session() as session:
            invoker = services.invoker(session)
            foreign = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.cancel",
                    principal_id=foreign_response.principal_id,
                    inputs={"work_id": work_id},
                )
            )
        assert foreign.outcome == "failure"
        assert "different principal" in foreign.error

        # The owner cancels.
        async with db_session() as session:
            invoker = services.invoker(session)
            own = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.cancel",
                    principal_id=principal_id,
                    inputs={"work_id": work_id},
                )
            )
            await session.commit()
        assert own.outcome == "success"
        assert own.outputs["cancelled"] is True

        item = await _get_work(work_id)
        assert item.status == "cancelled"

    async def test_list_shows_only_own_work(self, fresh_db, services) -> None:
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
        principal_id = response.principal_id

        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            listed = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.list",
                    principal_id=principal_id,
                    inputs={},
                )
            )
        assert listed.outcome == "success"
        assert listed.outputs["count"] == 0


class TestWorkRetrySemantics:
    async def test_failing_work_retries_then_dies_with_dead_letter(
        self, fresh_db, services, runner
    ) -> None:
        """Durable work must not fail silently: bounded retries, then dead
        + dead-letter row — the honest terminal state."""
        from wax.reliability.dead_letter import DeadLetterEntry

        call_count = 0

        async def failing_impl(inputs: dict, ctx: object) -> dict:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("transient infrastructure failure")

        services.capability_registry.register(
            CapabilityDescriptor(
                name="test.always_fail",
                description="Failing capability for retry tests",
                required_permission="capability.invoke:built_in",
                timeout_seconds=2.0,
            ),
            failing_impl,
        )

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())

        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            scheduled = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.schedule",
                    principal_id=response.principal_id,
                    inputs={
                        "payload": {
                            "capability_name": "test.always_fail",
                            "inputs": {},
                        },
                        "delay_seconds": 0,
                        "max_attempts": 2,
                    },
                )
            )
            await session.commit()
        work_id = scheduled.outputs["work_id"]

        # Attempt 1 → fails → pending (retry).
        await runner.run_once()
        item = await _get_work(work_id)
        assert item.status == "pending"
        assert item.attempts == 1

        # Attempt 2 → fails → dead (exhausted).
        await runner.run_once()
        item = await _get_work(work_id)
        assert item.status == "dead"
        assert item.attempts == 2
        assert "transient" in (item.last_error or "")

        async with db_session() as session:
            letters = (
                (
                    await session.execute(
                        select(DeadLetterEntry).where(DeadLetterEntry.kind == "work.capability")
                    )
                )
                .scalars()
                .all()
            )
        assert len(letters) == 1
        assert letters[0].attempts == 2

    async def test_expired_lease_is_reclaimed_and_counts_as_attempt(
        self, fresh_db, services
    ) -> None:
        """A worker that dies mid-work must not lose the item: an expired
        lease is reclaimable, and reclaiming consumes an attempt."""
        runner = WorkRunner(
            services,
            lease_seconds=0.05,
            retry_backoff_seconds=0.0,
            stale_execution_seconds=900.0,
        )
        runner.register_handler("capability", capability_handler)

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())

        async with db_session() as session:
            item = await WorkRepository(session).schedule(
                kind="capability",
                payload={"capability_name": "echo", "inputs": {"ok": 1}},
                wake_at=datetime.now(UTC) - timedelta(seconds=1),
                principal_id=response.principal_id,
                max_attempts=2,
            )
            # Simulate a dead worker holding an expired lease.
            item.status = "leased"
            item.lease_owner = "dead-worker"
            item.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
            item.attempts = 1
            await session.commit()
            work_id = item.id

        claimed = await runner.run_once()
        assert claimed == 1
        item = await _get_work(work_id)
        assert item.status == "succeeded"  # echo ran after reclaim
        assert item.attempts == 2

    async def test_reclaim_beyond_max_attempts_goes_dead(self, fresh_db, services) -> None:
        runner = WorkRunner(
            services,
            lease_seconds=0.05,
            retry_backoff_seconds=0.0,
            stale_execution_seconds=900.0,
        )
        runner.register_handler("capability", capability_handler)

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
            item = await WorkRepository(session).schedule(
                kind="capability",
                payload={"capability_name": "echo", "inputs": {}},
                wake_at=datetime.now(UTC) - timedelta(seconds=1),
                principal_id=response.principal_id,
                max_attempts=1,
            )
            item.status = "running"
            item.lease_owner = "dead-worker"
            item.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
            item.attempts = 1
            await session.commit()
            work_id = item.id

        await runner.run_once()
        item = await _get_work(work_id)
        assert item.status == "dead"


class TestRecoveryScan:
    async def test_stale_running_executions_are_reconciled(
        self, fresh_db, services, runner
    ) -> None:
        """The crash hole from audit Section 8: a process death mid-LLM left
        executions running and messages pending forever. The recovery scan
        reconciles both, making redelivery retryable."""
        from wax.state.bridge_models import ProcessedMessageRecord
        from wax.state.execution_models import ExecutionRecord
        from wax.state.identity_models import Principal

        async with db_session() as session:
            principal = Principal(
                id="01RECOVERYPRINCIPAL0000000", status="active", display_name="R"
            )
            session.add(principal)
            execution = ExecutionRecord(
                id="01RECOVERYEXECUTION000000",
                principal_id=principal.id,
                kind="single_turn",
                status="running",
                objective="old objective",
                started_at=datetime.now(UTC) - timedelta(seconds=2000),
            )
            session.add(execution)
            record = ProcessedMessageRecord(
                id="01RECOVERYMESSAGE0000000000",
                interface_kind="whatsapp",
                interface_message_id="wamid.recovery",
                principal_id=principal.id,
                execution_id=execution.id,
                outcome="pending",
                received_at=datetime.now(UTC) - timedelta(seconds=2000),
            )
            session.add(record)
            await session.commit()

        counts = await runner.recover_orphans()
        assert counts["failed_executions"] == 1

        async with db_session() as session:
            execution = await session.get(ExecutionRecord, execution.id)
            record = await session.get(ProcessedMessageRecord, record.id)
        assert execution.status == "failed"
        assert "restart" in (execution.error or "")
        assert record.outcome == "failed"


class TestMessageSendMechanism:
    async def test_send_through_delivery_router_respects_window_and_ownership(
        self, fresh_db, services
    ) -> None:
        """The reminder's delivery leg: message.send — with Meta's 24-hour
        window and identity ownership enforced by the RUNTIME."""
        delivered: list[tuple[str, str]] = []

        async def fake_sender(recipient: str, text: str) -> dict:
            delivered.append((recipient, text))
            return {"messages": [{"id": "wamid.fake"}]}

        services.delivery.register("whatsapp", fake_sender)

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
        principal_id = response.principal_id

        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="message.send",
                    principal_id=principal_id,
                    inputs={
                        "recipient_id": "+2348000000000",
                        "text": "your study break is over",
                    },
                )
            )
            await session.commit()
        assert result.outcome == "success", result.error
        assert delivered == [("+2348000000000", "your study break is over")]

    async def test_send_refuses_third_party_recipients(self, fresh_db, services) -> None:
        delivered: list[tuple[str, str]] = []

        async def fake_sender(recipient: str, text: str) -> dict:
            delivered.append((recipient, text))
            return {}

        services.delivery.register("whatsapp", fake_sender)

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())

        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="message.send",
                    principal_id=response.principal_id,
                    inputs={
                        "recipient_id": "+2349999999999",  # not the caller
                        "text": "spam",
                    },
                )
            )
        assert result.outcome == "failure"
        assert "third parties" in result.error
        assert delivered == []

    async def test_send_outside_24h_window_is_honestly_refused(self, fresh_db, services) -> None:
        """Meta policy: outside the window a template would be required.
        WAX has none — the runtime says so truthfully instead of faking."""
        delivered: list[tuple[str, str]] = []

        async def fake_sender(recipient: str, text: str) -> dict:
            delivered.append((recipient, text))
            return {}

        services.delivery.register("whatsapp", fake_sender)

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
            principal_id = response.principal_id
            # Age the message ledger beyond 24 hours.
            from wax.state.bridge_models import ProcessedMessageRecord

            record = (await session.execute(select(ProcessedMessageRecord))).scalar_one()
            record.received_at = datetime.now(UTC) - timedelta(hours=30)
            await session.commit()

        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="message.send",
                    principal_id=principal_id,
                    inputs={
                        "recipient_id": "+2348000000000",
                        "text": "hello from the past",
                    },
                )
            )
        assert result.outcome == "failure"
        assert "24-hour" in result.error
        assert "template" in result.error
        assert delivered == []


class TestOpenWorldComposition:
    async def test_reminder_without_a_reminder_service(self, fresh_db, services, runner) -> None:
        """THE composition test: a user message → the AI schedules durable
        work → the runtime wakes it → message.send delivers. No scheduler
        feature, no ReminderService, no TimerService — mechanisms only."""
        delivered: list[tuple[str, str]] = []

        async def fake_sender(recipient: str, text: str) -> dict:
            delivered.append((recipient, text))
            return {}

        services.delivery.register("whatsapp", fake_sender)

        # The "AI" requests: schedule a message.send in 0.2s, then confirm.
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(
                MockLLMProvider(
                    scripted_tool_calls=[
                        [
                            ToolCall(
                                id="call_schedule",
                                name="work.schedule",
                                arguments={
                                    "payload": {
                                        "capability_name": "message.send",
                                        "inputs": {
                                            "recipient_id": "+2348000000000",
                                            "text": "Reminder: stretch your legs",
                                        },
                                    },
                                    "delay_seconds": 0.2,
                                },
                            )
                        ]
                    ]
                )
            ),
            services=services,
        )

        async with db_session() as session:
            response = await bridge.process(
                session, _request(message_id="msg-remind-1", text="remind me soon")
            )
        assert response.status == RuntimeResponseStatus.SUCCESS

        # Work exists, not yet woken.
        async with db_session() as session:
            items = (
                (
                    await session.execute(
                        select(WorkItemRecord).where(
                            WorkItemRecord.principal_id == response.principal_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(items) == 1
        assert items[0].status == "pending"
        assert delivered == []

        # Time passes (0.2s), the runtime wakes the work.
        await asyncio.sleep(0.3)
        ran = await runner.run_once()
        assert ran == 1

        assert delivered == [("+2348000000000", "Reminder: stretch your legs")]
        item = await _get_work(items[0].id)
        assert item.status == "succeeded"
        assert item.result == {
            "sent": True,
            "interface": "whatsapp",
            "recipient_id": "+2348000000000",
        }
