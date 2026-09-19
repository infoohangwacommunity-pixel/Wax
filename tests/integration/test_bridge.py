"""Bridge integration tests — the most critical path in the system.

Tests the full flow: message → identity → idempotency → execution →
memory retrieval → model ↔ terminal loop → memory extraction → delivery.

Uses the mock provider so no real API keys are needed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select

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
from wax.state.continuity_models import ConversationRecord
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.execution_models import ExecutionRecord
from wax.state.identity_models import Principal, PrincipalCredential
from wax.state.memory_models import MemoryRecord
from wax.state.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings: Any):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    test_settings.__dict__["terminal_working_dir_root"] = "/tmp/wax-test-workspaces"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield test_settings
    await dispose_engine()


@pytest.fixture
def services(test_settings: Any):
    """Build services with a mock intelligence wired in.

    Tests that need a specific mock script should build their own
    services+bridge pair (see TestBridgeTerminalLoop). This fixture
    provides the default wired pair for identity/idempotency tests.
    """
    mock = MockLLMProvider()
    intel = IntelligenceService(mock)
    return RuntimeServices.build(test_settings, intelligence=intel)


@pytest.fixture
def bridge(services: Any):
    """A bridge backed by the services fixture's wired intelligence."""
    # services.intelligence is the mock wired in `services` fixture above.
    return RuntimeBridge(intelligence=services.require_intelligence(), services=services)


def _request(*, message_id: str = "msg-1", text: str = "Hello WAX", phone: str = "+1234567890"):
    return RuntimeRequest(
        interface_message_id=message_id,
        interface_kind=InterfaceKind.WHATSAPP,
        sender_interface_id=phone,
        sender_display_name="Test User",
        text=text,
        received_at=datetime.now(UTC),
    )


class TestBridgeIdentity:
    """Identity: the bridge creates a principal on first contact."""

    async def test_first_message_creates_principal(self, fresh_db, bridge) -> None:
        async with db_session() as session:
            response = await bridge.process(session, _request())
            await session.commit()

        assert response.status == RuntimeResponseStatus.SUCCESS
        assert response.principal_id is not None

        async with db_session() as session:
            principal = (await session.execute(select(Principal))).scalars().first()
            assert principal is not None
            cred = (
                (
                    await session.execute(
                        select(PrincipalCredential).where(
                            PrincipalCredential.kind == "whatsapp_phone"
                        )
                    )
                )
                .scalars()
                .first()
            )
            assert cred is not None
            assert cred.value == "+1234567890"

    async def test_returning_user_resolves_same_principal(self, fresh_db, bridge) -> None:
        async with db_session() as session:
            r1 = await bridge.process(session, _request(message_id="msg-1"))
            await session.commit()
        async with db_session() as session:
            r2 = await bridge.process(session, _request(message_id="msg-2"))
            await session.commit()

        assert r1.principal_id == r2.principal_id


class TestBridgeIdempotency:
    """Idempotency: duplicate messages are not re-processed."""

    async def test_duplicate_message_returns_duplicate_status(self, fresh_db, bridge) -> None:
        async with db_session() as session:
            r1 = await bridge.process(session, _request(message_id="msg-dup"))
            await session.commit()
        async with db_session() as session:
            r2 = await bridge.process(session, _request(message_id="msg-dup"))
            await session.commit()

        assert r1.status == RuntimeResponseStatus.SUCCESS
        assert r2.status == RuntimeResponseStatus.DUPLICATE
        assert r2.duplicate_of_execution_id == r1.execution_id


class TestBridgeExecution:
    """Execution: every message creates an execution record."""

    async def test_execution_created(self, fresh_db, bridge) -> None:
        async with db_session() as session:
            response = await bridge.process(session, _request())
            await session.commit()

        async with db_session() as session:
            execution = (
                (
                    await session.execute(
                        select(ExecutionRecord).where(ExecutionRecord.id == response.execution_id)
                    )
                )
                .scalars()
                .first()
            )
            assert execution is not None
            assert execution.status == "succeeded"


class TestBridgeConversation:
    """Conversation: a conversation is created and touched."""

    async def test_conversation_created(self, fresh_db, bridge) -> None:
        async with db_session() as session:
            await bridge.process(session, _request())
            await session.commit()

        async with db_session() as session:
            conv = (await session.execute(select(ConversationRecord))).scalars().first()
            assert conv is not None
            assert conv.status == "active"


class TestBridgeRateLimit:
    """Rate limiting: over-limit messages are rejected without calling the LLM."""

    async def test_rate_limit_kicks_in(self, fresh_db, test_settings) -> None:
        test_settings.__dict__["rate_limit_messages_per_hour"] = 3
        services = RuntimeServices.build(test_settings)
        mock = MockLLMProvider()
        intel = IntelligenceService(mock)
        bridge = RuntimeBridge(intelligence=intel, services=services)

        statuses = []
        for i in range(5):
            async with db_session() as session:
                r = await bridge.process(session, _request(message_id=f"msg-{i}"))
                await session.commit()
                statuses.append(r.status)

        # First 3 should succeed, 4th and 5th should be rate-limited
        assert statuses[0] == RuntimeResponseStatus.SUCCESS
        assert statuses[1] == RuntimeResponseStatus.SUCCESS
        assert statuses[2] == RuntimeResponseStatus.SUCCESS
        assert statuses[3] == RuntimeResponseStatus.RATE_LIMITED
        assert statuses[4] == RuntimeResponseStatus.RATE_LIMITED


class TestBridgeTerminalLoop:
    """The terminal loop: the AI can use the terminal tool."""

    async def test_terminal_tool_executes(self, fresh_db, services) -> None:
        # Script: first call requests terminal. After the terminal observation
        # comes back, the script is empty so the mock falls through to its
        # echo path (produces a final text response with no tool calls).
        mock = MockLLMProvider(
            scripted_tool_calls=[
                [ToolCall(id="tc-1", name="terminal", arguments={"command": "echo hello"})],
            ]
        )
        intel = IntelligenceService(mock)
        bridge = RuntimeBridge(intelligence=intel, services=services)

        async with db_session() as session:
            response = await bridge.process(session, _request(text="Run echo hello"))
            await session.commit()

        assert response.status == RuntimeResponseStatus.SUCCESS
        assert response.text  # should have some response text


class TestBridgeMemoryExtraction:
    """Memory: after an interaction, memories are extracted."""

    async def test_memory_extracted_after_interaction(self, fresh_db, services) -> None:
        # The mock provider echoes — the extraction will fall back to heuristic
        mock = MockLLMProvider()
        intel = IntelligenceService(mock)
        bridge = RuntimeBridge(intelligence=intel, services=services)

        async with db_session() as session:
            response = await bridge.process(
                session,
                _request(
                    text="My name is David and I'm preparing for my WAEC exam in October.",
                    message_id="msg-mem-1",
                ),
            )
            await session.commit()

        assert response.status == RuntimeResponseStatus.SUCCESS

        # Check that at least one memory was created
        async with db_session() as session:
            memories = (
                (
                    await session.execute(
                        select(MemoryRecord).where(
                            MemoryRecord.principal_id == response.principal_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(memories) >= 1


class TestBridgeReentry:
    """Durable re-entry: scheduled work wakes the intelligence."""

    async def test_run_reentry_returns_result(self, fresh_db, services) -> None:
        mock = MockLLMProvider()
        intel = IntelligenceService(mock)
        bridge = RuntimeBridge(intelligence=intel, services=services)

        # First, create a principal via a normal message
        async with db_session() as session:
            r = await bridge.process(session, _request(message_id="msg-pre"))
            await session.commit()
        principal_id = r.principal_id

        # Now simulate a re-entry
        from wax.runtime.work.reentry import ReentryRequest

        request = ReentryRequest(
            principal_id=principal_id,
            originating_execution_id=r.execution_id,
            work_item_id="test-work-1",
            prompt="Check if the exam date is still correct.",
            observation={"source": "runtime", "event": "scheduled_wake"},
        )

        result = await bridge.run_reentry(request)

        assert result.outcome == "succeeded"
        assert result.execution_id is not None
        assert result.response_text is not None


class TestBridgeExecutionRecovery:
    """Part 22: Execution recovery — checkpoint + resume after crash.

    The AI crashes mid-execution. The server restarts. The work runner
    finds the interrupted execution with a terminal-loop checkpoint and
    resumes it. The AI wakes up with the same message history and continues.
    """

    async def test_checkpoint_written_after_terminal_round(self, fresh_db, services) -> None:
        """After a terminal call, the execution's checkpoint is updated."""
        from sqlalchemy import select as sel

        from wax.state.execution_models import ExecutionRecord

        mock = MockLLMProvider(
            scripted_tool_calls=[
                [ToolCall(id="tc-1", name="terminal", arguments={"command": "echo hello"})],
            ]
        )
        intel = IntelligenceService(mock)
        bridge = RuntimeBridge(intelligence=intel, services=services)

        async with db_session() as session:
            response = await bridge.process(session, _request(text="Run echo hello"))
            await session.commit()

        # Verify a checkpoint was written
        async with db_session() as session:
            execution = (
                await session.execute(
                    sel(ExecutionRecord).where(ExecutionRecord.id == response.execution_id)
                )
            ).scalar_one()
            assert execution.checkpoint is not None
            assert "messages" in execution.checkpoint
            assert execution.checkpoint["round"] == 0

    async def test_resume_execution_from_checkpoint(self, fresh_db, services) -> None:
        """resume_execution loads the checkpoint and continues the loop."""
        from sqlalchemy import select as sel

        from wax.state.execution_models import ExecutionRecord

        # First execution: terminal call, then the "crash" happens (we just
        # don't complete it — we leave it in running state with a checkpoint).
        mock = MockLLMProvider(
            scripted_tool_calls=[
                [ToolCall(id="tc-1", name="terminal", arguments={"command": "echo hello"})],
                # The second call would produce a final response, but we
                # simulate a crash before it happens by NOT calling complete.
            ]
        )
        intel = IntelligenceService(mock)
        bridge = RuntimeBridge(intelligence=intel, services=services)

        async with db_session() as session:
            response = await bridge.process(session, _request(text="Run echo hello"))
            await session.commit()

        # The execution should be succeeded (the mock produced a final response
        # after the terminal call). But let's manually set it back to "running"
        # to simulate a crash, then test resume_execution.
        async with db_session() as session:
            execution = (
                await session.execute(
                    sel(ExecutionRecord).where(ExecutionRecord.id == response.execution_id)
                )
            ).scalar_one()
            execution.status = "running"
            execution.ended_at = None
            execution.error = None
            await session.commit()

        # Now resume — the bridge should load the checkpoint and continue
        # The mock's script is empty now, so it will produce a final response
        result = await bridge.resume_execution(response.execution_id)

        # The resume should have produced a response
        assert result is not None

        # The execution should now be completed
        async with db_session() as session:
            execution = (
                await session.execute(
                    sel(ExecutionRecord).where(ExecutionRecord.id == response.execution_id)
                )
            ).scalar_one()
            assert execution.status == "succeeded"

    async def test_resume_execution_without_checkpoint_returns_none(
        self, fresh_db, services
    ) -> None:
        """If there's no checkpoint, resume_execution returns None."""
        from wax.execution.repository import ExecutionRepository

        mock = MockLLMProvider()
        intel = IntelligenceService(mock)
        bridge = RuntimeBridge(intelligence=intel, services=services)

        # Create an execution with no checkpoint
        async with db_session() as session:
            exec_repo = ExecutionRepository(session)
            execution = await exec_repo.create(
                principal_id="01TESTPRINCIPAL000000000",
                kind="single_turn",
                objective="test",
            )
            await exec_repo.start(execution.id)
            await session.commit()
            exec_id = execution.id

        result = await bridge.resume_execution(exec_id)
        assert result is None
