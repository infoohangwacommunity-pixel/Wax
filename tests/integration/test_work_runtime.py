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
        # ADR-0035: the recovery layer now classifies the crash point and
        # produces a more specific error message. The execution had no
        # recorded steps, so it's classified as "crash before model call".
        assert "crash before model call" in (execution.error or "")
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
        """Vendor delivery policy: the WHATSAPP ADAPTER declares a 24-hour
        inbound-freshness window (Meta would require a template outside it;
        WAX has none) and the runtime enforces whatever the attached
        interface declares — generically — refusing truthfully instead of
        faking. No other interface carries this policy."""
        delivered: list[tuple[str, str]] = []

        async def fake_sender(recipient: str, text: str) -> dict:
            delivered.append((recipient, text))
            return {}

        from datetime import timedelta

        from wax.runtime.delivery import DeliveryPolicy

        services.delivery.register(
            "whatsapp",
            fake_sender,
            policy=DeliveryPolicy(
                inbound_freshness_window=timedelta(hours=24),
                freshness_note=(
                    "the 24-hour customer service window has closed and the "
                    "vendor requires an approved template message; no "
                    "template is registered on this deployment. Ask the user "
                    "to message WAX first."
                ),
            ),
        )

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


class TestEventWakeConditions:
    """Durable waiting beyond time (ADR-0011).

    Work can wait for a CONDITION — a named runtime signal in the event
    ledger — not only for a clock time. The runtime owns the ledger and
    the namespaces; the intelligence decides what to wait for. A reminder
    is still just the time-wake composition; "continue when the user
    replies" and "run when that work finishes" are event-wake compositions
    of the SAME mechanism. No feature was added; the primitive was
    generalized.
    """

    async def _schedule(self, services, principal_id: str, inputs: dict, capability: str = "echo"):
        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.schedule",
                    principal_id=principal_id,
                    inputs={
                        "payload": {"capability_name": capability, "inputs": {}},
                        **inputs,
                    },
                )
            )
            await session.commit()
        assert result.outcome == "success", result.error
        return result.outputs["work_id"]

    async def _principal(self, services) -> str:
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
        return response.principal_id

    async def test_event_work_does_not_wake_on_time_alone(self, fresh_db, services, runner) -> None:
        """An event-wake item whose floor time has passed but whose signal
        never fired stays pending — time alone does not satisfy a
        condition."""
        from wax.runtime.work.signals import SignalRepository

        pid = await self._principal(services)
        work_id = await self._schedule(services, pid, {"wake_event": "test.never.fired"})

        await asyncio.sleep(0.05)
        ran = await runner.run_once()
        assert ran == 0
        item = await _get_work(work_id)
        assert item.status == "pending"

        # And the ledger is empty for that name.
        async with db_session() as session:
            assert (
                await SignalRepository(session).has_signal_since(
                    "test.never.fired", after=datetime.now(UTC) - timedelta(seconds=1)
                )
                is None
            )

    async def test_event_work_wakes_when_signal_emitted(self, fresh_db, services, runner) -> None:
        from wax.runtime.work.signals import SignalRepository

        pid = await self._principal(services)
        work_id = await self._schedule(services, pid, {"wake_event": "test.go"})

        ran = await runner.run_once()
        assert ran == 0  # no signal yet

        async with db_session() as session:
            await SignalRepository(session).emit(
                "test.go", payload={"reason": "condition met"}, emitted_by="test"
            )
            await session.commit()

        ran = await runner.run_once()
        assert ran == 1
        item = await _get_work(work_id)
        assert item.status == "succeeded"
        assert item.result == {"echo": {}}

    async def test_signal_broadcast_wakes_all_waiters(self, fresh_db, services, runner) -> None:
        """Signals are broadcast facts: every waiter on the name wakes, each
        consuming the event independently via its own watermark."""
        from wax.runtime.work.signals import SignalRepository

        pid = await self._principal(services)
        a = await self._schedule(services, pid, {"wake_event": "test.broadcast"})
        b = await self._schedule(services, pid, {"wake_event": "test.broadcast"})

        async with db_session() as session:
            await SignalRepository(session).emit("test.broadcast", emitted_by="test")
            await session.commit()

        ran = await runner.run_once()
        assert ran == 2
        assert (await _get_work(a)).status == "succeeded"
        assert (await _get_work(b)).status == "succeeded"

    async def test_watermark_prevents_retroactive_wake(self, fresh_db, services, runner) -> None:
        """A signal emitted BEFORE the wait began never fires it. Waiting
        starts at scheduling time; old facts are not new conditions."""
        from wax.runtime.work.signals import SignalRepository

        pid = await self._principal(services)

        # A signal arrives first...
        async with db_session() as session:
            await SignalRepository(session).emit("test.stale", emitted_by="test")
            await session.commit()

        # ...then work starts waiting for it. The stale fact must NOT wake it.
        work_id = await self._schedule(services, pid, {"wake_event": "test.stale"})
        ran = await runner.run_once()
        assert ran == 0
        assert (await _get_work(work_id)).status == "pending"

        # A NEW emission (strictly after the watermark) does wake it.
        await asyncio.sleep(0.01)
        async with db_session() as session:
            await SignalRepository(session).emit("test.stale", emitted_by="test")
            await session.commit()
        ran = await runner.run_once()
        assert ran == 1
        assert (await _get_work(work_id)).status == "succeeded"

    async def test_retry_reconsumes_same_signal_at_least_once(
        self, fresh_db, services, runner
    ) -> None:
        """Handler failure after a legitimate wake retries on the SAME
        signal (at-least-once, consistent with lease-reclaim semantics)."""
        from wax.capabilities.contracts import CapabilityDescriptor
        from wax.runtime.work.signals import SignalRepository

        calls = {"n": 0}

        async def flaky(inputs, ctx):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient failure")
            return {"attempt": calls["n"]}

        services.capability_registry.register(
            CapabilityDescriptor(
                name="test.flaky",
                description="fails once",
                version="1.0.0",
                input_schema={"type": "object", "properties": {}},
                required_permission="capability.invoke:built_in",
                timeout_seconds=5.0,
                idempotent=False,
                is_destructive=False,
            ),
            flaky,
        )

        pid = await self._principal(services)
        work_id = await self._schedule(
            services, pid, {"wake_event": "test.flaky.go"}, capability="test.flaky"
        )

        async with db_session() as session:
            await SignalRepository(session).emit("test.flaky.go", emitted_by="test")
            await session.commit()

        assert await runner.run_once() == 1  # attempt 1 fails
        item = await _get_work(work_id)
        assert item.status == "pending"  # backoff (0s in fixture) → retryable
        assert "transient failure" in (item.last_error or "")

        assert await runner.run_once() == 1  # attempt 2 on the SAME signal
        item = await _get_work(work_id)
        assert item.status == "succeeded"
        assert item.result == {"attempt": 2}

    async def test_wait_deadline_expires_honestly(self, fresh_db, services, runner) -> None:
        """A condition that never fires and has a deadline dies with an
        honest 'condition not met' — not silently, not fabricated. This is
        a lifecycle outcome, not an execution failure: no dead-letter row."""
        from wax.reliability.dead_letter import DeadLetterRepository

        pid = await self._principal(services)
        work_id = await self._schedule(
            services, pid, {"wake_event": "test.deadline", "expires_in_seconds": 0.05}
        )

        await asyncio.sleep(0.1)
        assert await runner.run_once() == 0  # sweeps the expired wait
        item = await _get_work(work_id)
        assert item.status == "dead"
        assert "condition not met" in (item.last_error or "")

        # A lifecycle outcome, not an execution failure: no dead-letter row.
        async with db_session() as session:
            rows = await DeadLetterRepository(session).list_recent(limit=10)
        assert all(
            letter.payload is None or letter.payload.get("work_id") != work_id for letter in rows
        )

    async def test_dependency_chain_via_work_succeeded_signal(
        self, fresh_db, services, runner
    ) -> None:
        """'Run B when A finishes' is a composition: B waits on
        work.succeeded:<A>; the runner announces A's terminal state on the
        ledger; the next pass wakes B. No workflow engine, no new feature."""
        pid = await self._principal(services)
        a = await self._schedule(
            services,
            pid,
            {
                "payload": {"capability_name": "echo", "inputs": {"message": "A"}},
                "delay_seconds": 0.05,
            },
        )
        b = await self._schedule(services, pid, {"wake_event": f"work.succeeded:{a}"})

        # Pass 1: A wakes and succeeds (announcing its signal in the same
        # transaction as the status change).
        await asyncio.sleep(0.1)  # let A's delay elapse
        assert await runner.run_once() == 1
        assert (await _get_work(a)).status == "succeeded"
        assert (await _get_work(b)).status == "pending"  # not yet — signal
        # was emitted during pass 1, but B's claim query in that pass ran
        # before A ran. One more pass:
        assert await runner.run_once() == 1
        assert (await _get_work(b)).status == "succeeded"

    async def test_signal_emit_capability_gates_and_reserved_namespaces(
        self, fresh_db, services
    ) -> None:
        """signal.emit crosses the full gate chain; runtime-owned
        namespaces cannot be forged by the intelligence (they can only be
        waited on)."""
        from wax.capabilities.contracts import CapabilityInvocationRequest

        pid = await self._principal(services)

        async with db_session() as session:
            invoker = services.invoker(session)

            ok = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="signal.emit",
                    principal_id=pid,
                    inputs={"name": "external.payment.received", "payload": {"amount": 1}},
                )
            )
            assert ok.outcome == "success", ok.error
            assert ok.outputs["name"] == "external.payment.received"

            for forged in ("interface.message:someone", "work.succeeded:xyz"):
                bad = await invoker.invoke(
                    CapabilityInvocationRequest(
                        capability_name="signal.emit",
                        principal_id=pid,
                        inputs={"name": forged},
                    )
                )
                assert bad.outcome == "failure", forged
                assert "runtime-owned" in bad.error

            malformed = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="signal.emit",
                    principal_id=pid,
                    inputs={"name": "bad name with spaces!"},
                )
            )
            assert malformed.outcome == "failure"

    async def test_scheduled_work_carries_execution_traceability(self, fresh_db, services) -> None:
        """G4 fix: work scheduled during an execution links back to it
        (execution → objective traceability), instead of dropping the
        context on the floor."""
        from wax.capabilities.contracts import CapabilityInvocationRequest

        pid = await self._principal(services)
        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.schedule",
                    principal_id=pid,
                    inputs={
                        "payload": {"capability_name": "echo", "inputs": {}},
                        "delay_seconds": 60,
                    },
                    request_id="01EXECUTIONTRACE01",
                )
            )
            await session.commit()
        assert result.outcome == "success"
        item = await _get_work(result.outputs["work_id"])
        assert item.execution_id == "01EXECUTIONTRACE01"

    async def test_bridge_emits_interface_message_signal(self, fresh_db, services) -> None:
        """A processed inbound message announces
        interface.message:<principal> on the ledger — the runtime-owned
        fact 'this human interacted now' that continue-on-reply work waits
        for."""
        from wax.runtime.work.signals import SignalRepository

        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            response = await bridge.process(session, _request())
        assert response.status is RuntimeResponseStatus.SUCCESS

        async with db_session() as session:
            hit = await SignalRepository(session).has_signal_since(
                f"interface.message:{response.principal_id}",
                after=datetime.now(UTC) - timedelta(minutes=5),
            )
        assert hit is not None
        assert hit.payload["interface"] == "whatsapp"
        assert hit.emitted_by == "bridge"


class TestWorkRequeue:
    """G5: dead work is a recovery state, not a graveyard (ADR-0011 §requeue).

    When retries are exhausted (e.g. a provider outage), the objective must
    not be lost forever: the owner can requeue the dead item as a fresh,
    provenance-linked attempt."""

    async def _make_dead_work(self, services, principal_id: str) -> str:
        """Schedule echo work, then drive it through exhaustion to dead."""
        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.schedule",
                    principal_id=principal_id,
                    inputs={
                        "payload": {"capability_name": "echo", "inputs": {}},
                        "delay_seconds": 0.05,
                        "max_attempts": 2,
                    },
                )
            )
            await session.commit()
        assert result.outcome == "success"
        work_id = result.outputs["work_id"]

        runner = WorkRunner(
            services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
            stale_execution_seconds=900.0,
        )
        from wax.runtime.work.runner import WorkExecutionError

        async def failing_handler(services_, item):
            raise WorkExecutionError("dependency gone")

        runner.register_handler("capability", failing_handler)

        await asyncio.sleep(0.1)
        await runner.run_once()  # attempt 1 → fail → retry
        await runner.run_once()  # attempt 2 → fail → dead (+ dead letter)
        item = await _get_work(work_id)
        assert item.status == "dead"
        return work_id

    async def _invoke(self, services, principal_id: str, inputs: dict):
        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.requeue",
                    principal_id=principal_id,
                    inputs=inputs,
                )
            )
            await session.commit()
        return result

    async def test_requeue_revives_dead_work_with_provenance(self, fresh_db, services) -> None:
        pid = await TestEventWakeConditions._principal(self, services)
        dead_id = await self._make_dead_work(services, pid)

        result = await self._invoke(services, pid, {"work_id": dead_id})
        assert result.outcome == "success", result.error
        assert result.outputs["requeued_from"] == dead_id
        assert result.outputs["status"] == "pending"

        # The requeued item runs with the ORIGINAL payload and succeeds.
        runner = WorkRunner(
            services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
            stale_execution_seconds=900.0,
        )
        runner.register_handler("capability", capability_handler)
        ran = await runner.run_once()
        assert ran == 1
        item = await _get_work(result.outputs["work_id"])
        assert item.status == "succeeded"
        assert item.payload.get("requeued_from") == dead_id
        assert item.payload.get("capability_name") == "echo"

    async def test_requeue_is_ownership_checked(self, fresh_db, services) -> None:
        owner = await TestEventWakeConditions._principal(self, services)
        bridge = RuntimeBridge(
            intelligence=IntelligenceService(MockLLMProvider()), services=services
        )
        async with db_session() as session:
            other = await bridge.process(
                session, _request(message_id="msg-requeue-other", sender_id="+2348000000009")
            )
        stranger = other.principal_id

        dead_id = await self._make_dead_work(services, owner)
        result = await self._invoke(services, stranger, {"work_id": dead_id})
        assert result.outcome == "failure"
        assert "different principal" in result.error

    async def test_requeue_refuses_live_and_succeeded_work(self, fresh_db, services) -> None:
        pid = await TestEventWakeConditions._principal(self, services)
        from wax.capabilities.contracts import CapabilityInvocationRequest

        async with db_session() as session:
            invoker = services.invoker(session)
            scheduled = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="work.schedule",
                    principal_id=pid,
                    inputs={
                        "payload": {"capability_name": "echo", "inputs": {}},
                        "delay_seconds": 60,
                    },
                )
            )
            await session.commit()
        assert scheduled.outcome == "success"
        live_id = scheduled.outputs["work_id"]

        result = await self._invoke(services, pid, {"work_id": live_id})
        assert result.outcome == "failure"
        assert "Only dead work" in result.error
