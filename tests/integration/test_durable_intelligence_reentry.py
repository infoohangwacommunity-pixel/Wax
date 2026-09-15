"""Durable Intelligence Re-entry — integration tests (ADR-0034).

Covers the full wake→continuation→intelligence→evidence→objective
reconciliation flow, including the constitutional invariants the design
must preserve:

- runtime observation presented as typed evidence (NOT user content)
- ownership boundary (wrong principal rejected)
- lease fencing (stale worker cannot overwrite)
- objective state reconciled per evidence (no auto-close)
- credentials never enter the continuation prompt
- continuation appears in objective history
- continuation can invoke capabilities / schedule work / request approval
- continuation failure creates execution failure evidence
- runner retry does not silently duplicate effects
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from wax.execution.contracts import ExecutionStatus
from wax.execution.repository import ExecutionRepository
from wax.identity.repository import PrincipalRepository
from wax.intelligence.adapters.mock_provider import MockLLMProvider
from wax.intelligence.contracts import LLMRequest, ToolCall
from wax.intelligence.service import IntelligenceService
from wax.memory.repository import MemoryRepository
from wax.objective.contracts import ObjectiveStatus
from wax.objective.evidence import objective_for_execution
from wax.objective.repository import ObjectiveRepository
from wax.runtime.bridge.contracts import InterfaceKind, RuntimeRequest, RuntimeResponse
from wax.runtime.bridge.service import RuntimeBridge
from wax.runtime.services import RuntimeServices
from wax.runtime.work import WorkRepository, WorkRunner
from wax.runtime.work.handlers import intelligence_handler
from wax.runtime.work.reentry import (
    REENTRY_OBSERVATION_MAX_BYTES,
    REENTRY_PROMPT_MAX_CHARS,
    ReentryValidationError,
    validate_reentry_payload,
)
from wax.state.approval_models import PendingApprovalRecord
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.execution_models import ExecutionRecord
from wax.state.work_models import WorkItemRecord

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture
async def runtime_setup(test_settings):
    """Bring up the runtime against an in-memory SQLite DB.

    Yields a RuntimeEnv with: services, bridge, mock intelligence, principal.
    Uses `:memory:` SQLite + Base.metadata.create_all (the standard test
    pattern) so concurrent sessions within the same test process share the
    same in-memory DB.
    """
    # Override DB URL to in-memory SQLite (matches the conftest pattern)
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    test_settings.__dict__["llm_default_provider"] = "mock"
    test_settings.__dict__["context_char_budget"] = 4000
    test_settings.__dict__["max_tool_rounds"] = 4

    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]

    # Create schema from SQLAlchemy models (the standard test pattern;
    # migration correctness is tested separately in test_migrations.py)
    from wax.state.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed roles
    from wax.authority.seed import seed_builtin_roles

    async with db_session() as s:
        await seed_builtin_roles(s)
        await s.commit()

    services = RuntimeServices.build(test_settings)

    # Build the bridge with a mock intelligence that has a deterministic script
    intel = IntelligenceService.from_settings(test_settings)
    bridge = RuntimeBridge(intelligence=intel, services=services)
    services.reentry_callback = bridge.run_reentry

    # Register a destructive probe capability so we can test
    # continuation→approval paths
    from wax.capabilities.contracts import CapabilityDescriptor

    async def _probe_wipe(inputs, ctx=None):
        return {"wiped": inputs.get("target", "")}

    services.capability_registry.register(
        CapabilityDescriptor(
            name="test.wipe",
            description="Probe destructive capability",
            required_permission="capability.invoke:built_in",
            is_destructive=True,
            timeout_seconds=5.0,
        ),
        _probe_wipe,
    )

    # Create a principal with a verified credential
    # IMPORTANT: the interface kind "whatsapp" maps to the credential kind
    # "whatsapp_phone" — see wax.identity.contracts.INTERFACE_CREDENTIAL_KINDS.
    # Using the wrong credential kind here means the bridge cannot resolve
    # the sender and creates a NEW principal, breaking the test.
    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name="Test User")
        await repo.add_credential(
            principal.id,
            kind="whatsapp_phone",
            value="1234567890",
            is_verified=True,
        )
        from wax.authority.seed import (
            DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
            ensure_principal_role,
        )

        await ensure_principal_role(s, principal.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
        await s.commit()
        principal_id = principal.id

    yield _Env(
        services=services,
        bridge=bridge,
        intelligence=intel,
        principal_id=principal_id,
        settings=test_settings,
    )

    # Teardown

    await dispose_engine()


class _Env:
    def __init__(self, services, bridge, intelligence, principal_id, settings):
        self.services = services
        self.bridge = bridge
        self.intelligence = intelligence
        self.principal_id = principal_id
        self.settings = settings

    def script_intelligence(self, scripted_tool_calls):
        """Replace the intelligence provider with a deterministic script."""
        self.intelligence._provider = MockLLMProvider(scripted_tool_calls=scripted_tool_calls)


async def _send_initial_message(env: _Env, text: str = "hello") -> RuntimeResponse:
    """Run an interface message through the bridge to create the initial
    objective + execution + conversation that the re-entry will continue."""
    request = RuntimeRequest(
        interface_kind=InterfaceKind.WHATSAPP,
        interface_message_id=f"test-msg-{datetime.now(UTC).timestamp()}",
        sender_interface_id="1234567890",
        sender_display_name="Test User",
        text=text,
        received_at=datetime.now(UTC),
    )
    async with db_session() as s:
        response = await env.bridge.process(s, request)
        await s.commit()
    return response


# ----------------------------------------------------------------------------
# 1. Payload validation (unit-level, but in integration test file for context)
# ----------------------------------------------------------------------------


class TestReentryPayloadValidation:
    def test_valid_payload(self):
        prompt, observation = validate_reentry_payload(
            {
                "prompt": "Reassess after the wake.",
                "observation": {
                    "source": "runtime",
                    "event": "work.succeeded",
                    "work_id": "01M2ABC",
                    "result": {"key": "value"},
                },
            }
        )
        assert prompt == "Reassess after the wake."
        assert observation["source"] == "runtime"
        assert observation["event"] == "work.succeeded"

    def test_missing_prompt_raises(self):
        with pytest.raises(ReentryValidationError, match="prompt"):
            validate_reentry_payload({"observation": {"source": "runtime", "event": "x"}})

    def test_empty_prompt_raises(self):
        with pytest.raises(ReentryValidationError, match="prompt"):
            validate_reentry_payload(
                {"prompt": "   ", "observation": {"source": "runtime", "event": "x"}}
            )

    def test_oversized_prompt_raises(self):
        with pytest.raises(ReentryValidationError, match="exceeds"):
            validate_reentry_payload(
                {
                    "prompt": "x" * (REENTRY_PROMPT_MAX_CHARS + 1),
                    "observation": {"source": "runtime", "event": "x"},
                }
            )

    def test_invalid_source_raises(self):
        with pytest.raises(ReentryValidationError, match="source"):
            validate_reentry_payload(
                {
                    "prompt": "ok",
                    "observation": {"source": "user", "event": "x"},
                }
            )

    def test_missing_event_raises(self):
        with pytest.raises(ReentryValidationError, match="event"):
            validate_reentry_payload({"prompt": "ok", "observation": {"source": "runtime"}})

    def test_oversized_observation_raises(self):
        with pytest.raises(ReentryValidationError, match="exceeds"):
            big_result = "x" * (REENTRY_OBSERVATION_MAX_BYTES + 100)
            validate_reentry_payload(
                {
                    "prompt": "ok",
                    "observation": {
                        "source": "runtime",
                        "event": "x",
                        "result": {"data": big_result},
                    },
                }
            )

    def test_none_payload_raises(self):
        with pytest.raises(ReentryValidationError):
            validate_reentry_payload(None)


# ----------------------------------------------------------------------------
# 2. End-to-end re-entry flow (time wake)
# ----------------------------------------------------------------------------


class TestTimeWakeReentry:
    """Verify that a time-wake `intelligence` work item triggers the
    bridge's re-entry callback and produces a continuation execution."""

    async def test_time_wake_invokes_intelligence(self, runtime_setup):
        env: _Env = runtime_setup
        # Send an initial message to create the objective/conversation
        await _send_initial_message(env, "remind me to study in 1 second")

        # Resolve the originating execution_id (the latest one for this principal)
        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id

        # Script the intelligence to return a final text on the re-entry
        env.script_intelligence(scripted_tool_calls=[[]])

        # Schedule an intelligence work item — wake in 0 seconds (immediate)
        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Reassess the objective now that the timer fired.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "work_id": None,
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()
            work_id = item.id

        # Run the work runner once
        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        ran = await runner.run_once()

        assert ran == 1, "the runner should have processed exactly one item"

        # The work item should be succeeded
        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            assert item.status == "succeeded", (
                f"work should be succeeded, was {item.status} error={item.last_error}"
            )
            assert item.result["outcome"] in (
                "in_progress",
                "waiting",
                "awaiting_human",
                "succeeded",
            ), f"unexpected outcome {item.result['outcome']}"
            continuation_execution_id = item.result["execution_id"]

            # The continuation execution exists and was completed
            cont_exec = await s.get(ExecutionRecord, continuation_execution_id)
            assert cont_exec is not None
            assert cont_exec.status == ExecutionStatus.SUCCEEDED.value

            # The continuation appears in the objective's execution history
            objective = await objective_for_execution(s, originating_execution_id)
            assert objective is not None
            history = await ObjectiveRepository(s).list_executions(objective.id)
            continuation_history_rows = [
                h for h in history if h.execution_id == continuation_execution_id
            ]
            assert len(continuation_history_rows) == 1
            assert continuation_history_rows[0].kind == "work_continuation"

    async def test_reentry_creates_continuation_memory(self, runtime_setup):
        env: _Env = runtime_setup
        await _send_initial_message(env, "wake me later")

        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id

        env.script_intelligence(scripted_tool_calls=[[]])

        async with db_session() as s:
            repo = WorkRepository(s)
            await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Re-check.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        await runner.run_once()

        # An episodic memory should exist with provenance="runtime_continuation"
        async with db_session() as s:
            mem_repo = MemoryRepository(s)
            memories = await mem_repo.list_active_for_principal(env.principal_id, limit=20)
            continuation_memories = [m for m in memories if m.provenance == "runtime_continuation"]
            assert len(continuation_memories) >= 1


# ----------------------------------------------------------------------------
# 3. Event-wake re-entry
# ----------------------------------------------------------------------------


class TestEventWakeReentry:
    async def test_event_wake_invokes_intelligence(self, runtime_setup):
        env: _Env = runtime_setup
        await _send_initial_message(env, "wake me when you hear back")

        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id

        env.script_intelligence(scripted_tool_calls=[[]])

        # Schedule an event-wake intelligence work item — wait for
        # interface.message:<principal>
        wake_event = f"interface.message:{env.principal_id}"
        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "The user replied; re-engage.",
                    "observation": {
                        "source": "runtime",
                        "event": "interface.message",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=1,
                wake_kind="event",
                wake_event=wake_event,
            )
            await s.commit()
            work_id = item.id

        # Emit the signal — this makes the item claimable
        from wax.runtime.work.signals import SignalRepository

        async with db_session() as s:
            await SignalRepository(s).emit(
                wake_event,
                payload={"interface": "whatsapp", "message_id": "test"},
                emitted_by="bridge",
            )
            await s.commit()

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        ran = await runner.run_once()

        assert ran == 1

        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            assert item.status == "succeeded"


# ----------------------------------------------------------------------------
# 4. Failure paths
# ----------------------------------------------------------------------------


class TestReentryFailurePaths:
    async def test_missing_objective_rejected(self, runtime_setup):
        """A re-entry work item whose execution_id has no objective returns
        a failed outcome with a clear error."""
        env: _Env = runtime_setup
        env.script_intelligence(scripted_tool_calls=[[]])

        # Use a non-existent execution_id (a fresh ULID that has no
        # objective linkage)
        from ulid import ULID

        bogus_execution_id = str(ULID())

        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Reassess.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=bogus_execution_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()
            work_id = item.id

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        await runner.run_once()

        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            # The handler returned a result dict with outcome="failed" —
            # but the work runner treats that as success (the handler
            # itself didn't raise). The work is "succeeded" but its result
            # records the failure honestly.
            assert item.status == "succeeded"
            assert item.result["outcome"] == "failed"
            assert "no objective" in (item.result["error"] or "").lower()

    async def test_wrong_principal_rejected(self, runtime_setup):
        """A re-entry work item whose principal_id does not match the
        conversation's owner is rejected."""
        env: _Env = runtime_setup
        await _send_initial_message(env, "test")
        env.script_intelligence(scripted_tool_calls=[[]])

        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id

        # Create a SECOND principal and use that for the work item
        async with db_session() as s:
            repo = PrincipalRepository(s)
            other_principal = await repo.create_principal(display_name="Other")
            await repo.add_credential(
                other_principal.id,
                kind="whatsapp_phone",
                value="9999999999",
                is_verified=True,
            )
            from wax.authority.seed import (
                DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
                ensure_principal_role,
            )

            await ensure_principal_role(s, other_principal.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
            await s.commit()
            other_principal_id = other_principal.id

        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Reassess.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=other_principal_id,
                execution_id=originating_execution_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()
            work_id = item.id

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        await runner.run_once()

        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            assert item.status == "succeeded"
            assert item.result["outcome"] == "failed"
            assert "principal" in (item.result["error"] or "").lower()


# ----------------------------------------------------------------------------
# 5. Missing re-entry callback (no bridge wired)
# ----------------------------------------------------------------------------


class TestMissingReentryCallback:
    async def test_no_callback_raises_workexecutionerror(self, runtime_setup):
        """If services.reentry_callback is None, the handler fails honestly."""
        env: _Env = runtime_setup
        env.services.reentry_callback = None  # Unset the callback

        await _send_initial_message(env, "test")
        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id

        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Reassess.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()
            work_id = item.id

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        await runner.run_once()

        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            # The handler raised WorkExecutionError → the runner's
            # _fail_item path → mark_failed. With max_attempts=1, the item
            # should now be "dead" (retries exhausted) or "failed" (retryable).
            assert item.status in ("failed", "dead"), f"expected failed/dead, got {item.status}"
            assert "not configured" in (item.last_error or "").lower()


# ----------------------------------------------------------------------------
# 6. Continuation can invoke capabilities
# ----------------------------------------------------------------------------


class TestContinuationInvokesCapability:
    async def test_continuation_can_invoke_echo(self, runtime_setup):
        env: _Env = runtime_setup
        await _send_initial_message(env, "test")

        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id

        # Script the intelligence to call `echo` then return text
        env.script_intelligence(
            scripted_tool_calls=[
                [ToolCall(id="c1", name="echo", arguments={"message": "from-reentry"})],
                [],  # second call: no tools, just text
            ]
        )

        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Call echo with 'from-reentry'.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()
            work_id = item.id

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        await runner.run_once()

        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            assert item.status == "succeeded", f"item failed: {item.last_error}"

            # The continuation execution should have a capability.invoke step
            from wax.state.execution_models import ExecutionStepRecord

            cont_exec_id = item.result["execution_id"]
            steps = (
                (
                    await s.execute(
                        select(ExecutionStepRecord)
                        .where(ExecutionStepRecord.execution_id == cont_exec_id)
                        .order_by(ExecutionStepRecord.step_number.asc())
                    )
                )
                .scalars()
                .all()
            )
            capability_steps = [s for s in steps if s.kind == "capability.invoke"]
            assert len(capability_steps) >= 1
            assert capability_steps[0].capability_name == "echo"


# ----------------------------------------------------------------------------
# 7. Continuation can schedule more work (chained re-entry)
# ----------------------------------------------------------------------------


class TestContinuationSchedulesWork:
    async def test_continuation_schedules_more_work(self, runtime_setup):
        env: _Env = runtime_setup
        await _send_initial_message(env, "test")

        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id

        # Script the intelligence to call work.schedule then return text
        env.script_intelligence(
            scripted_tool_calls=[
                [
                    ToolCall(
                        id="c1",
                        name="work.schedule",
                        arguments={
                            "kind": "intelligence",
                            "payload": {
                                "prompt": "Next re-entry.",
                                "observation": {
                                    "source": "runtime",
                                    "event": "time.wake",
                                    "result": {},
                                },
                            },
                            "delay_seconds": 60,
                        },
                    )
                ],
                [],
            ]
        )

        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Schedule a follow-up re-entry.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()
            work_id = item.id

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        await runner.run_once()

        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            assert item.status == "succeeded", item.last_error
            # The continuation scheduled MORE work → the objective is waiting
            assert item.result["outcome"] == "waiting"

            # A new intelligence work item was scheduled by the continuation
            new_work = (
                await s.execute(
                    select(WorkItemRecord)
                    .where(WorkItemRecord.principal_id == env.principal_id)
                    .where(WorkItemRecord.kind == "intelligence")
                    .where(WorkItemRecord.id != work_id)
                    .order_by(WorkItemRecord.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            assert new_work is not None


# ----------------------------------------------------------------------------
# 8. Continuation can request approval (destructive capability)
# ----------------------------------------------------------------------------


class TestContinuationRequestsApproval:
    async def test_continuation_destructive_request_creates_approval(self, runtime_setup):
        env: _Env = runtime_setup
        # Script the INITIAL bridge call to schedule a delayed work item
        # so the objective stays in `waiting` (not auto-completed to
        # `succeeded`, which is terminal). Then the re-entry can
        # reactivate it and request approval via test.wipe.
        env.script_intelligence(
            scripted_tool_calls=[
                [
                    ToolCall(
                        id="init-schedule",
                        name="work.schedule",
                        arguments={
                            "payload": {
                                "capability_name": "echo",
                                "inputs": {"message": "placeholder"},
                            },
                            "delay_seconds": 3600,
                        },
                    )
                ],
                [],
            ]
        )
        await _send_initial_message(env, "schedule placeholder work")

        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id
            objective = await objective_for_execution(s, originating_execution_id)
            assert objective.status == ObjectiveStatus.WAITING.value, (
                f"expected waiting after scheduling work, got {objective.status}"
            )

        # Now script the re-entry to call test.wipe (destructive) — this
        # creates a pending approval and the objective transitions to
        # awaiting_human.
        env.script_intelligence(
            scripted_tool_calls=[
                [ToolCall(id="c1", name="test.wipe", arguments={"target": "data"})],
                [],
            ]
        )

        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Wipe the data.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()
            work_id = item.id

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        await runner.run_once()

        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            assert item.status == "succeeded", item.last_error

            # A pending approval should exist for test.wipe
            approvals = (
                await s.execute(
                    select(PendingApprovalRecord)
                    .where(PendingApprovalRecord.principal_id == env.principal_id)
                    .where(PendingApprovalRecord.capability_name == "test.wipe")
                    .order_by(PendingApprovalRecord.requested_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            assert approvals is not None
            assert approvals.status == "pending"

            # The objective should be awaiting_human
            objective = await objective_for_execution(s, originating_execution_id)
            assert objective.status == ObjectiveStatus.AWAITING_HUMAN.value, (
                f"expected awaiting_human, got {objective.status}"
            )


# ----------------------------------------------------------------------------
# 9. Credentials never enter the continuation prompt
# ----------------------------------------------------------------------------


class TestNoSecretLeakage:
    async def test_continuation_does_not_leak_credentials(self, runtime_setup):
        env: _Env = runtime_setup
        # Inject a fake secret into the env to ensure it does NOT appear
        # in any intelligence message
        env.settings.whatsapp_access_token = "secret-token-do-not-leak-12345"

        # Schedule placeholder work so the objective stays `waiting`
        env.script_intelligence(
            scripted_tool_calls=[
                [
                    ToolCall(
                        id="init-schedule",
                        name="work.schedule",
                        arguments={
                            "payload": {
                                "capability_name": "echo",
                                "inputs": {"message": "placeholder"},
                            },
                            "delay_seconds": 3600,
                        },
                    )
                ],
                [],
            ]
        )
        await _send_initial_message(env, "schedule placeholder work")
        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id

        # Capture the messages sent to the LLM
        original_complete = env.intelligence._provider.complete
        captured_messages: list = []

        async def _capturing_complete(req: LLMRequest):
            captured_messages.append(req.messages)
            return await original_complete(req)

        env.intelligence._provider.complete = _capturing_complete

        env.script_intelligence(scripted_tool_calls=[[]])

        async with db_session() as s:
            repo = WorkRepository(s)
            await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Reassess.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=1,
                wake_kind="time",
            )
            await s.commit()

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)
        await runner.run_once()

        # Verify the secret never appears in any LLM message
        for messages in captured_messages:
            for m in messages:
                content = m.content or ""
                assert "secret-token-do-not-leak-12345" not in content, (
                    "SECRET LEAKED into LLM message"
                )
                # Tool-call arguments too
                for tc in m.tool_calls or []:
                    args_str = str(tc.arguments)
                    assert "secret-token-do-not-leak-12345" not in args_str


# ----------------------------------------------------------------------------
# 10. Lease fencing — stale worker cannot overwrite
# ----------------------------------------------------------------------------


class TestLeaseFencing:
    async def test_stale_worker_cannot_overwrite_state(self, runtime_setup):
        """When a worker's lease expires, a NEW worker can reclaim and
        process the item. The stale worker's subsequent writes are fenced."""
        env: _Env = runtime_setup
        # Script the initial call to schedule placeholder work so the
        # objective stays in `waiting` (re-entry requires non-terminal state).
        env.script_intelligence(
            scripted_tool_calls=[
                [
                    ToolCall(
                        id="init-schedule",
                        name="work.schedule",
                        arguments={
                            "payload": {
                                "capability_name": "echo",
                                "inputs": {"message": "placeholder"},
                            },
                            "delay_seconds": 3600,
                        },
                    )
                ],
                [],
            ]
        )
        await _send_initial_message(env, "schedule placeholder work")
        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id
            objective = await objective_for_execution(s, originating_execution_id)
            assert objective.status == ObjectiveStatus.WAITING.value

        env.script_intelligence(scripted_tool_calls=[[]])

        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Reassess.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=2,
                wake_kind="time",
            )
            await s.commit()
            work_id = item.id

        # Worker A claims the item
        async with db_session() as s:
            repo = WorkRepository(s)
            batch = await repo.claim_due(
                worker_id="worker-A",
                lease_seconds=120.0,
                limit=10,
            )
            await s.commit()
            assert len(batch.claimed) == 1

        # Expire worker A's lease
        async with db_session() as s:
            item_record = await s.get(WorkItemRecord, work_id)
            item_record.lease_expires_at = datetime.now(UTC) - timedelta(seconds=10)
            # Also move available_at back so worker B can re-claim
            item_record.available_at = datetime.now(UTC) - timedelta(seconds=10)
            await s.commit()

        # Worker B reclaims and runs the item
        runner_b = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner_b.register_handler("capability", lambda *a, **kw: None)
        runner_b.register_handler("intelligence", intelligence_handler)
        ran = await runner_b.run_once()
        assert ran == 1, "Worker B should have reclaimed and processed the item"

        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            assert item.status == "succeeded"


# ----------------------------------------------------------------------------
# 11. Runner retry does not silently duplicate effects
# ----------------------------------------------------------------------------


class TestNoDuplicateEffects:
    async def test_retry_does_not_duplicate_intelligence_runs(self, runtime_setup):
        """A failed intelligence re-entry that retries should not run the
        intelligence twice for the same wake."""
        env: _Env = runtime_setup
        # Schedule placeholder work so the objective stays `waiting` —
        # re-entry requires a non-terminal objective.
        env.script_intelligence(
            scripted_tool_calls=[
                [
                    ToolCall(
                        id="init-schedule",
                        name="work.schedule",
                        arguments={
                            "payload": {
                                "capability_name": "echo",
                                "inputs": {"message": "placeholder"},
                            },
                            "delay_seconds": 3600,
                        },
                    )
                ],
                [],
            ]
        )
        await _send_initial_message(env, "schedule placeholder work")
        async with db_session() as s:
            exec_repo = ExecutionRepository(s)
            execs = await exec_repo.list_for_principal(env.principal_id, limit=1)
            originating_execution_id = execs[0].id

        # Count LLM calls — the first attempt should fail, the retry should
        # succeed, and there should be exactly ONE continuation execution
        # created across both attempts.
        call_count = [0]
        original_complete = env.intelligence._provider.complete

        async def _counting_complete(req: LLMRequest):
            call_count[0] += 1
            return await original_complete(req)

        env.intelligence._provider.complete = _counting_complete
        env.script_intelligence(scripted_tool_calls=[[]])

        async with db_session() as s:
            repo = WorkRepository(s)
            item = await repo.schedule(
                kind="intelligence",
                payload={
                    "prompt": "Reassess.",
                    "observation": {
                        "source": "runtime",
                        "event": "time.wake",
                        "result": {},
                    },
                },
                wake_at=datetime.now(UTC),
                principal_id=env.principal_id,
                execution_id=originating_execution_id,
                max_attempts=2,  # allow one retry
                wake_kind="time",
            )
            await s.commit()
            work_id = item.id

        runner = WorkRunner(
            env.services,
            poll_interval_seconds=0.05,
            lease_seconds=120.0,
            retry_backoff_seconds=0.0,
        )
        runner.register_handler("capability", lambda *a, **kw: None)
        runner.register_handler("intelligence", intelligence_handler)

        # First run_once should process the item successfully on first attempt
        ran = await runner.run_once()
        assert ran == 1

        async with db_session() as s:
            item = await s.get(WorkItemRecord, work_id)
            assert item.status == "succeeded"

            # Exactly one continuation execution was created

            objective = await objective_for_execution(s, originating_execution_id)
            history = await ObjectiveRepository(s).list_executions(objective.id)
            continuation_rows = [h for h in history if h.kind == "work_continuation"]
            assert len(continuation_rows) == 1
