"""Integration tests for Phase H (Execution).

Tests verify:
- Execution lifecycle (pending → running → succeeded/failed/cancelled)
- Invalid status transitions are rejected
- Steps are numbered sequentially and append-only
- get_latest_succeeded_step returns the resume point
- Persistence: execution survives across sessions
- Multiple executions don't interfere
- Resumability: after crash, latest succeeded step is the resume point
"""

from __future__ import annotations

import pytest

from wax.execution.contracts import ExecutionKind, StepStatus
from wax.execution.repository import ExecutionRepository
from wax.identity.repository import PrincipalRepository
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
async def principal_id(fresh_db) -> str:
    async with db_session() as session:
        repo = PrincipalRepository(session)
        p = await repo.create_principal(display_name="Execution Test User")
        await session.commit()
        return p.id


class TestExecutionLifecycle:
    async def test_create_returns_pending_execution(self, principal_id) -> None:
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id,
                kind=ExecutionKind.SINGLE_TURN,
                objective="Help me understand photosynthesis",
            )
            await session.commit()
            assert execution.id is not None
            assert execution.status == "pending"
            assert execution.kind == "single_turn"
            assert execution.objective == "Help me understand photosynthesis"

    async def test_pending_to_running(self, principal_id) -> None:
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id,
                kind=ExecutionKind.SINGLE_TURN,
                objective="test",
            )
            ok = await repo.start(execution.id)
            await session.commit()
            assert ok

        async with db_session() as session:
            repo = ExecutionRepository(session)
            e = await repo.get(execution.id)
            assert e.status == "running"
            assert e.started_at is not None

    async def test_running_to_succeeded(self, principal_id) -> None:
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id, kind=ExecutionKind.SINGLE_TURN, objective="test"
            )
            await repo.start(execution.id)
            ok = await repo.complete(execution.id, checkpoint={"final_step": 5})
            await session.commit()
            assert ok

        async with db_session() as session:
            repo = ExecutionRepository(session)
            e = await repo.get(execution.id)
            assert e.status == "succeeded"
            assert e.ended_at is not None
            assert e.checkpoint == {"final_step": 5}

    async def test_running_to_failed(self, principal_id) -> None:
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id, kind=ExecutionKind.SINGLE_TURN, objective="test"
            )
            await repo.start(execution.id)
            ok = await repo.fail(execution.id, error="LLM provider timeout")
            await session.commit()
            assert ok

        async with db_session() as session:
            repo = ExecutionRepository(session)
            e = await repo.get(execution.id)
            assert e.status == "failed"
            assert e.error == "LLM provider timeout"

    async def test_running_to_cancelled(self, principal_id) -> None:
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id, kind=ExecutionKind.SINGLE_TURN, objective="test"
            )
            await repo.start(execution.id)
            ok = await repo.cancel(execution.id)
            await session.commit()
            assert ok

        async with db_session() as session:
            repo = ExecutionRepository(session)
            e = await repo.get(execution.id)
            assert e.status == "cancelled"

    async def test_invalid_transition_rejected(self, principal_id) -> None:
        """Cannot go from succeeded back to running."""
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id, kind=ExecutionKind.SINGLE_TURN, objective="test"
            )
            await repo.start(execution.id)
            await repo.complete(execution.id)
            ok = await repo.start(execution.id)  # invalid
            await session.commit()
            assert not ok


class TestExecutionSteps:
    async def test_steps_numbered_sequentially(self, principal_id) -> None:
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id,
                kind=ExecutionKind.AGENT_LOOP,
                objective="multi-step test",
            )
            await session.commit()

            s1 = await repo.record_step(execution.id, kind="model_call", outputs={"msg": "step 1"})
            s2 = await repo.record_step(
                execution.id, kind="capability_invoke", capability_name="echo", outputs={"echo": {}}
            )
            s3 = await repo.record_step(execution.id, kind="model_call", outputs={"msg": "step 3"})
            await session.commit()

            assert s1.step_number == 1
            assert s2.step_number == 2
            assert s3.step_number == 3

    async def test_list_steps_returns_in_order(self, principal_id) -> None:
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id, kind=ExecutionKind.AGENT_LOOP, objective="test"
            )
            await repo.record_step(execution.id, kind="model_call", outputs={"step": 1})
            await repo.record_step(execution.id, kind="model_call", outputs={"step": 2})
            await repo.record_step(execution.id, kind="model_call", outputs={"step": 3})
            await session.commit()

        async with db_session() as session:
            repo = ExecutionRepository(session)
            steps = await repo.list_steps(execution.id)
            assert len(steps) == 3
            assert steps[0].step_number == 1
            assert steps[2].step_number == 3

    async def test_get_latest_succeeded_step(self, principal_id) -> None:
        """Resume point: the latest step that succeeded."""
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id, kind=ExecutionKind.AGENT_LOOP, objective="resume test"
            )
            await repo.record_step(execution.id, kind="model_call", outputs={"step": 1})
            await repo.record_step(execution.id, kind="model_call", outputs={"step": 2})
            await repo.record_step(
                execution.id,
                kind="model_call",
                outputs={"step": 3},
                status=StepStatus.FAILED,
                error="LLM error",
            )
            await session.commit()

        async with db_session() as session:
            repo = ExecutionRepository(session)
            latest = await repo.get_latest_succeeded_step(execution.id)
            assert latest is not None
            assert latest.step_number == 2  # NOT 3 (which failed)


class TestExecutionResumability:
    """The critical test: can we reconstruct state after a 'crash'?"""

    async def test_execution_survives_session_close(self, principal_id) -> None:
        """Execution persists across sessions (simulates process restart)."""
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id,
                kind=ExecutionKind.AGENT_LOOP,
                objective="long-running objective",
            )
            await repo.start(execution.id)
            await repo.record_step(
                execution.id, kind="model_call", outputs={"planned": ["a", "b", "c"]}
            )
            await repo.record_step(
                execution.id,
                kind="capability_invoke",
                capability_name="echo",
                outputs={"result": "done a"},
            )
            await session.commit()
            exec_id = execution.id

        # New session — simulates process restart
        async with db_session() as session:
            repo = ExecutionRepository(session)
            e = await repo.get(exec_id)
            assert e is not None
            assert e.status == "running"
            steps = await repo.list_steps(exec_id)
            assert len(steps) == 2
            latest = await repo.get_latest_succeeded_step(exec_id)
            assert latest is not None
            assert latest.step_number == 2
            assert latest.outputs["result"] == "done a"

    async def test_failed_execution_can_retry(self, principal_id) -> None:
        """A failed execution can be restarted (running is allowed from failed)."""
        async with db_session() as session:
            repo = ExecutionRepository(session)
            execution = await repo.create(
                principal_id=principal_id, kind=ExecutionKind.AGENT_LOOP, objective="retry test"
            )
            await repo.start(execution.id)
            await repo.fail(execution.id, error="temporary outage")
            await session.commit()

        async with db_session() as session:
            repo = ExecutionRepository(session)
            ok = await repo.start(execution.id)  # retry
            await session.commit()
            assert ok
            e = await repo.get(execution.id)
            assert e.status == "running"
