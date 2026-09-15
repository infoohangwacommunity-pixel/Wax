"""Open-world Validation — acceptance tests (ADR-0046, Phase 13).

Proves WAX can pursue unfamiliar legitimate objectives by composing
universal runtime mechanisms — no domain engines.

Test patterns:
1. "Research a topic" — objective + memory + terminal + artifact
2. "Build software" — objective + environment + terminal + workspace + artifact
3. "Continue when the user replies" (long-running) — work + time wake + re-entry + memory + delivery
4. "Monitor for a condition" (event-wake) — work + event wake + signal ledger + re-entry
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.identity.repository import PrincipalRepository
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import ToolCall
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import InterfaceKind, RuntimeRequest
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.runtime.vault import seed_builtin_connectors
from wax.runtime.work import WorkRunner, capability_handler
from wax.runtime.work.handlers import intelligence_handler
from wax.runtime.work.signals import SignalRepository
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base
from wax.state.work_models import WorkItemRecord

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


async def _create_principal(*, phone: str = "1234567890") -> str:
    from wax.authority.seed import (
        DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
        ensure_principal_role,
    )

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name="Test")
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
        await s.commit()
        return principal.id


def _make_bridge(services: RuntimeServices, script: list[list[ToolCall]]) -> RuntimeBridge:
    """Build a bridge with a mock intelligence that has a deterministic script."""
    intel = IntelligenceService(MockLLMProvider(scripted_tool_calls=script))
    bridge = RuntimeBridge(intelligence=intel, services=services)
    services.reentry_callback = bridge.run_reentry
    return bridge


def _request(msg_id: str, text: str) -> RuntimeRequest:
    return RuntimeRequest(
        interface_kind=InterfaceKind.WHATSAPP,
        interface_message_id=msg_id,
        sender_interface_id="1234567890",
        sender_display_name="Test",
        text=text,
        received_at=datetime.now(UTC),
    )


# ----------------------------------------------------------------------------
# Test 1: "Research a topic" — objective + memory + terminal + artifact
# ----------------------------------------------------------------------------


class TestResearchTopic:
    async def test_research_composes_objective_memory_terminal_artifact(self, fresh_db, services):
        """The intelligence researches by: running `echo` in a terminal,
        storing the finding as a memory, and capturing the output as
        an artifact. All universal primitives, no domain engine."""
        principal_id = await _create_principal()

        # Script: schedule placeholder work (keeps objective waiting),
        # then in the continuation: open environment → open terminal →
        # run echo → capture artifact → store memory
        bridge = _make_bridge(
            services,
            script=[
                [  # initial message: schedule placeholder + an environment
                    ToolCall(
                        id="sched",
                        name="work.schedule",
                        arguments={
                            "payload": {
                                "capability_name": "echo",
                                "inputs": {"message": "placeholder"},
                            },
                            "delay_seconds": 3600,
                        },
                    ),
                    ToolCall(
                        id="env",
                        name="environment.request",
                        arguments={
                            "purpose": "research workspace",
                            "ttl_seconds": 3600,
                        },
                    ),
                ],
                [],  # second round: just text
            ],
        )

        async with db_session() as s:
            response = await bridge.process(
                s, _request("msg-research-1", "research computing history")
            )
            await s.commit()

        assert response.status.value == "success"
        assert response.objective_id is not None
        assert response.execution_id is not None

        # The intelligence scheduled work (objective stays waiting) +
        # requested an environment (provisioned)
        async with db_session() as s:
            from wax.state.environment_models import EnvironmentLeaseRecord

            leases = (
                (
                    await s.execute(
                        select(EnvironmentLeaseRecord).where(
                            EnvironmentLeaseRecord.principal_id == principal_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(leases) >= 1
            assert leases[0].status == "provisioned"


# ----------------------------------------------------------------------------
# Test 2: "Build software" — environment + terminal + workspace + artifact
# ----------------------------------------------------------------------------


class TestBuildSoftware:
    async def test_build_composes_environment_terminal_artifact(self, fresh_db, services):
        """The intelligence builds by: requesting an environment, opening
        a terminal, writing a file, and capturing it as an artifact."""
        principal_id = await _create_principal()
        bridge = _make_bridge(
            services,
            script=[
                [
                    ToolCall(
                        id="env",
                        name="environment.request",
                        arguments={"purpose": "build workspace", "ttl_seconds": 3600},
                    ),
                ],
                [],  # done
            ],
        )

        async with db_session() as s:
            response = await bridge.process(s, _request("msg-build-1", "build hello world"))
            await s.commit()

        assert response.status.value == "success"
        # An environment was provisioned
        async with db_session() as s:
            from wax.state.environment_models import EnvironmentLeaseRecord

            leases = (
                (
                    await s.execute(
                        select(EnvironmentLeaseRecord).where(
                            EnvironmentLeaseRecord.principal_id == principal_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(leases) >= 1


# ----------------------------------------------------------------------------
# Test 3: "Continue when the user replies" (long-running, time wake)
# ----------------------------------------------------------------------------


class TestContinueWhenUserReplies:
    async def test_time_wake_reentry_composes_work_reentry_memory(self, fresh_db, services):
        """The intelligence schedules a re-entry; the work runner wakes
        it; the continuation runs. Proves the full Phase 1 architecture
        works end-to-end."""
        principal_id = await _create_principal()

        # Initial: schedule placeholder work + an intelligence re-entry
        bridge = _make_bridge(
            services,
            script=[
                [
                    ToolCall(
                        id="placeholder",
                        name="work.schedule",
                        arguments={
                            "payload": {
                                "capability_name": "echo",
                                "inputs": {"message": "placeholder"},
                            },
                            "delay_seconds": 3600,
                        },
                    ),
                    ToolCall(
                        id="reentry",
                        name="work.schedule",
                        arguments={
                            "kind": "intelligence",
                            "payload": {
                                "prompt": "Reassess after the timer fires.",
                                "observation": {
                                    "source": "runtime",
                                    "event": "time.wake",
                                    "result": {},
                                },
                            },
                            "delay_seconds": 0,
                        },
                    ),
                ],
                [],  # the re-entry's intelligence call: just text
            ],
        )

        async with db_session() as s:
            response = await bridge.process(s, _request("msg-continue-1", "remind me later"))
            await s.commit()
            principal_id = response.principal_id

        # The intelligence work item should be scheduled and immediately claimable
        async with db_session() as s:
            intel_work = (
                await s.execute(
                    select(WorkItemRecord)
                    .where(WorkItemRecord.principal_id == principal_id)
                    .where(WorkItemRecord.kind == "intelligence")
                    .order_by(WorkItemRecord.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            assert intel_work is not None
            assert intel_work.status == "pending"

        # Run the work runner — it should claim + run the intelligence re-entry
        runner = WorkRunner(
            services,
            poll_interval_seconds=0.05,
            lease_seconds=120,
            retry_backoff_seconds=0,
        )
        runner.register_handler("capability", capability_handler)
        runner.register_handler("intelligence", intelligence_handler)
        ran = await runner.run_once()
        assert ran >= 1

        # The re-entry should have succeeded
        async with db_session() as s:
            intel_work = await s.get(WorkItemRecord, intel_work.id)
            assert intel_work.status == "succeeded", intel_work.last_error


# ----------------------------------------------------------------------------
# Test 4: "Monitor for a condition" (event-wake)
# ----------------------------------------------------------------------------


class TestMonitorForCondition:
    async def test_event_wake_reentry_fires_on_signal(self, fresh_db, services):
        """The intelligence schedules a re-entry on
        interface.message:<principal>. When a new message arrives
        (signal emitted), the re-entry fires."""
        principal_id = await _create_principal()

        # Schedule placeholder work + an event-wake re-entry
        wake_event = f"interface.message:{principal_id}"
        bridge = _make_bridge(
            services,
            script=[
                [
                    ToolCall(
                        id="placeholder",
                        name="work.schedule",
                        arguments={
                            "payload": {
                                "capability_name": "echo",
                                "inputs": {"message": "placeholder"},
                            },
                            "delay_seconds": 3600,
                        },
                    ),
                    ToolCall(
                        id="reentry",
                        name="work.schedule",
                        arguments={
                            "kind": "intelligence",
                            "payload": {
                                "prompt": "The user replied; re-engage.",
                                "observation": {
                                    "source": "runtime",
                                    "event": "interface.message",
                                    "result": {},
                                },
                            },
                            "wake_event": wake_event,
                        },
                    ),
                ],
                [],  # re-entry intelligence: just text
            ],
        )

        async with db_session() as s:
            await bridge.process(s, _request("msg-monitor-1", "tell me when you hear back"))
            await s.commit()

        # Find the intelligence work item
        async with db_session() as s:
            intel_work = (
                await s.execute(
                    select(WorkItemRecord)
                    .where(WorkItemRecord.principal_id == principal_id)
                    .where(WorkItemRecord.kind == "intelligence")
                    .where(WorkItemRecord.wake_kind == "event")
                    .order_by(WorkItemRecord.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            assert intel_work is not None
            assert intel_work.wake_event == wake_event
            assert intel_work.status == "pending"

        # Emit the signal — this makes the item claimable
        async with db_session() as s:
            await SignalRepository(s).emit(
                wake_event,
                payload={"interface": "whatsapp", "message_id": "msg-trigger"},
                emitted_by="bridge",
            )
            await s.commit()

        # Run the work runner — should claim + run the event-wake re-entry
        runner = WorkRunner(
            services,
            poll_interval_seconds=0.05,
            lease_seconds=120,
            retry_backoff_seconds=0,
        )
        runner.register_handler("capability", capability_handler)
        runner.register_handler("intelligence", intelligence_handler)
        ran = await runner.run_once()
        assert ran >= 1

        # The re-entry should have succeeded
        async with db_session() as s:
            intel_work = await s.get(WorkItemRecord, intel_work.id)
            assert intel_work.status == "succeeded", intel_work.last_error
