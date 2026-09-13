"""Open-world validation — objectives nobody designed features for.

The test is NOT "does WAX have a feature for this?" The test is: "can the
runtime provide a general mechanism through which intelligence can pursue
this objective?" Every scenario below is a COMPOSITION of runtime
mechanisms (durable waiting, signals, memory lifecycle, provisioning,
isolation) driven by a scripted intelligence through the REAL bridge, the
REAL gate chain, and the REAL work runner. No scenario has a dedicated
runtime feature; if one did, that would be an architecture failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from ulid import ULID

from wax.authority.seed import seed_builtin_roles
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import ToolCall
from wax.intelligence.service import IntelligenceService
from wax.runtime.bridge.contracts import InterfaceKind, RuntimeRequest
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.runtime.work import WorkRunner, capability_handler
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base


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


def _request(message_id: str, text: str) -> RuntimeRequest:
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id="+2348000000777",
        sender_display_name="Open World",
        text=text,
        received_at=datetime.now(UTC),
    )


def _bridge(services: RuntimeServices, script: list[list[ToolCall]]) -> RuntimeBridge:
    return RuntimeBridge(
        intelligence=IntelligenceService(MockLLMProvider(scripted_tool_calls=script)),
        services=services,
    )


def _call(call_id: str, name: str, args: dict) -> ToolCall:
    return ToolCall(id=call_id, name=name, arguments=args)


async def _run_runner_once(services: RuntimeServices) -> int:
    runner = WorkRunner(
        services,
        poll_interval_seconds=0.05,
        lease_seconds=120.0,
        retry_backoff_seconds=0.0,
        stale_execution_seconds=900.0,
    )
    runner.register_handler("capability", capability_handler)
    return await runner.run_once()


class TestContinueWhenUserReplies:
    """'Ping me when I reply / continue the work on my next message.'

    No notification feature exists. The intelligence composes:
    work.schedule(wake_event="interface.message:<principal>") — a WAIT on
    a runtime-owned fact. The user's next inbound message satisfies it."""

    async def test_work_waits_for_the_human_then_continues(self, fresh_db, services) -> None:
        # Establish the principal first (plain message, no script).
        setup = _bridge(services, [])
        async with db_session() as session:
            response = await setup.process(session, _request("ow-0", "hello"))
        pid = response.principal_id

        # The AI schedules continuation work on the user's NEXT message.
        script = [
            [
                _call(
                    "c1",
                    "work.schedule",
                    {
                        "wake_event": f"interface.message:{pid}",
                        "payload": {
                            "capability_name": "echo",
                            "inputs": {"message": "welcome back, continuing our work"},
                        },
                    },
                )
            ],
        ]  # final text turn
        waiting = _bridge(services, script)
        async with db_session() as session:
            await waiting.process(session, _request("ow-1", "remind yourself when I reply"))

        # Nothing runs until the human actually replies: time passing does
        # not satisfy a condition.
        assert await _run_runner_once(services) == 0

        # The human replies — a NEW message through the REAL webhook path.
        reply = _bridge(services, [])
        async with db_session() as session:
            await reply.process(session, _request("ow-2", "I'm back"))

        # The runner correlates the waiting item against the signal ledger.
        assert await _run_runner_once(services) == 1

        from sqlalchemy import select

        from wax.state.work_models import WorkItemRecord

        async with db_session() as session:
            items = (
                (
                    await session.execute(
                        select(WorkItemRecord).where(
                            WorkItemRecord.principal_id == pid,
                            WorkItemRecord.status == "succeeded",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(items) == 1
        assert items[0].result == {"echo": {"message": "welcome back, continuing our work"}}


class TestMemoryLifecycleThroughConversation:
    """'Remember this' → 'actually it changed' → 'give me the durable
    takeaway.' The runtime preserves evidence + lifecycle; the model
    decides meaning. No rule engine, no hardcoded categories."""

    async def test_store_revise_consolidate(self, fresh_db, services) -> None:
        from sqlalchemy import select

        from wax.state.memory_models import MemoryRecord

        pid_setup = _bridge(services, [])
        async with db_session() as session:
            response = await pid_setup.process(session, _request("ow-m0", "hi"))
        pid = response.principal_id

        # 1. The model stores an observation.
        store = _bridge(
            services,
            [
                [
                    _call(
                        "m1",
                        "memory.store",
                        {
                            "kind": "episodic",
                            "content": {"fact": "user's exam is on May 20"},
                            "summary": "Exam May 20",
                        },
                    )
                ],
            ],
        )
        async with db_session() as session:
            await store.process(session, _request("ow-m1", "remember my exam date"))
        async with db_session() as session:
            stored = (
                (
                    await session.execute(
                        select(MemoryRecord).where(
                            MemoryRecord.principal_id == pid,
                            MemoryRecord.status == "active",
                            MemoryRecord.summary == "Exam May 20",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(stored) == 1
        first_id = stored[0].id

        # 2. The model REVISES it (supersedes — the old record leaves
        # retrieval but stays for audit).
        revise = _bridge(
            services,
            [
                [
                    _call(
                        "m2",
                        "memory.store",
                        {
                            "content": {"fact": "user's exam moved to June 2"},
                            "summary": "Exam June 2",
                            "supersedes": first_id,
                        },
                    )
                ],
            ],
        )
        async with db_session() as session:
            await revise.process(session, _request("ow-m2", "the exam moved"))

        # 3. The model CONSOLIDATES the revised fact into durable knowledge.
        async with db_session() as session:
            current = (
                (
                    await session.execute(
                        select(MemoryRecord).where(
                            MemoryRecord.principal_id == pid,
                            MemoryRecord.status == "active",
                            MemoryRecord.summary == "Exam June 2",
                        )
                    )
                )
                .scalars()
                .all()
            )
        current_ids = [m.id for m in current]
        consolidate = _bridge(
            services,
            [
                [
                    _call(
                        "m3",
                        "memory.consolidate",
                        {
                            "source_ids": current_ids,
                            "content": {"durable": "user is preparing for an exam in June"},
                            "summary": "Preparing for a June exam",
                            "kind": "semantic",
                        },
                    )
                ],
            ],
        )
        async with db_session() as session:
            await consolidate.process(session, _request("ow-m3", "summarize what you know"))

        async with db_session() as session:
            records = (
                (
                    await session.execute(
                        select(MemoryRecord).where(MemoryRecord.principal_id == pid)
                    )
                )
                .scalars()
                .all()
            )
        durable = [r for r in records if r.provenance == "consolidation"]
        assert len(durable) == 1
        assert durable[0].status == "active"
        # The chain is honest: the revision record was superseded BY the
        # durable representation; the original was superseded by the
        # revision. Supersession links form the audit trail.
        revised = next(r for r in records if r.summary == "Exam June 2")
        assert revised.superseded_by == durable[0].id
        original = next(r for r in records if r.summary == "Exam May 20")
        assert original.superseded_by == revised.id


class TestEnvironmentAcquisition:
    """'Analyze data I drop in a scratch space' — provision a workspace,
    execute code against it, remember the outcome. The acquisition flow is
    the mission's §17 loop: need → exists? → provision → use → truthful
    result. Code execution is authority-gated (opt-in), proving the
    security boundary holds INSIDE the composition."""

    async def test_provision_execute_remember(self, fresh_db, services) -> None:
        from wax.authority.seed import get_role_by_name
        from wax.state.authority_models import PrincipalRole

        setup = _bridge(services, [])
        async with db_session() as session:
            response = await setup.process(session, _request("ow-e0", "hi"))
        pid = response.principal_id

        # Opt-in authority: the deployment grants code execution to THIS
        # principal (trust boundary is explicit, not ambient).
        async with db_session() as session:
            admin = await get_role_by_name(session, "admin")
            session.add(PrincipalRole(id=str(ULID()), principal_id=pid, role_id=admin.id))
            await session.commit()

        # Script: provision → run python in the workspace (writes a file,
        # reads it back) → store the outcome as memory → final answer.
        script = [
            [_call("e1", "scratch.workspace", {"ttl_seconds": 600})],
            [
                _call(
                    "e2",
                    "code.run",
                    {
                        "language": "python",
                        "code": (
                            "with open('analysis.txt', 'w') as f:\n"
                            "    f.write('3 data points, mean=41.7')\n"
                            "print(open('analysis.txt').read())\n"
                        ),
                    },
                )
            ],
            [
                _call(
                    "e3",
                    "memory.store",
                    {
                        "content": {
                            "outcome": "analysis artifact produced: 3 data points, mean=41.7"
                        },
                        "summary": "Analysis complete: mean 41.7",
                        "kind": "episodic",
                    },
                )
            ],
        ]
        acquisition = _bridge(services, script)
        async with db_session() as session:
            result = await acquisition.process(
                session, _request("ow-e1", "analyze my data in a scratch workspace")
            )
        assert result.status.value == "success", result.text

        # The whole chain left honest evidence behind.
        from sqlalchemy import select

        from wax.state.memory_models import MemoryRecord
        from wax.state.provisioning_models import ProvisionedResourceRecord

        async with db_session() as session:
            workspace = (await session.execute(select(ProvisionedResourceRecord))).scalars().all()
            assert any(r.kind == "scratch_dir" for r in workspace)
            memory = (
                (
                    await session.execute(
                        select(MemoryRecord).where(MemoryRecord.principal_id == pid)
                    )
                )
                .scalars()
                .all()
            )
            assert any("mean=41.7" in str(m.content) for m in memory)
