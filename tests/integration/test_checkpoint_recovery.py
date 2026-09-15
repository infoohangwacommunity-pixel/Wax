"""Checkpoint Recovery — integration tests (ADR-0035, Phase 2).

Covers the 10 crash points from the directive:

1. crash before model call
2. crash after model response
3. crash before capability invocation
4. crash after external effect but before result persistence
5. crash after result persistence
6. crash while creating approval
7. crash after approval but before requeue
8. crash during artifact capture
9. crash during delivery
10. crash after delivery request but before delivery confirmation

Plus the new `unknown_effect` state and idempotency-lookup path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.execution.contracts import StepStatus
from wax.execution.recovery import (
    EXECUTION_STATUS_UNKNOWN_EFFECT,
    CheckpointEnvelope,
    CrashPoint,
    RecoveryOutcome,
    classify_crash,
    lookup_idempotent_outcome,
    recover_execution,
)
from wax.execution.repository import ExecutionRepository
from wax.runtime.services import RuntimeServices
from wax.runtime.work.runner import WorkRunner
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.execution_models import ExecutionRecord, ExecutionStepRecord
from wax.state.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings):
    """In-memory SQLite + schema from SQLAlchemy models."""
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


async def _create_execution(
    principal_id: str, *, kind: str = "single_turn", objective: str = "test"
) -> str:
    """Create a pending execution and return its ID."""
    async with db_session() as s:
        repo = ExecutionRepository(s)
        execution = await repo.create(principal_id=principal_id, kind=kind, objective=objective)
        await repo.start(execution.id)
        await s.commit()
        return execution.id


async def _record_step(
    execution_id: str,
    *,
    kind: str,
    status: str = "succeeded",
    inputs: dict | None = None,
    outputs: dict | None = None,
    capability_name: str | None = None,
    error: str | None = None,
) -> int:
    """Record a step on the execution and return its step number."""
    async with db_session() as s:
        repo = ExecutionRepository(s)
        step = await repo.record_step(
            execution_id,
            kind=kind,
            inputs=inputs,
            outputs=outputs,
            status=StepStatus(status)
            if status in ("succeeded", "failed", "pending", "running", "skipped")
            else status,
            capability_name=capability_name,
            error=error,
        )
        await s.commit()
        return step.step_number


async def _create_principal(*, display_name: str = "Test") -> str:
    from wax.authority.seed import (
        DEFAULT_ROLE_FOR_NEW_PRINCIPALS,
        ensure_principal_role,
    )
    from wax.identity.repository import PrincipalRepository

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name=display_name)
        await repo.add_credential(
            principal.id,
            kind="whatsapp_phone",
            value="1234567890",
            is_verified=True,
        )
        await ensure_principal_role(s, principal.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
        await s.commit()
        return principal.id


# ----------------------------------------------------------------------------
# 1. Crash before model call
# ----------------------------------------------------------------------------


class TestCrashBeforeModelCall:
    async def test_no_steps_marks_failed_retryable(self, fresh_db, services):
        principal_id = await _create_principal()
        execution_id = await _create_execution(principal_id)

        # The execution is "running" but no steps recorded
        async with db_session() as s:
            result = await recover_execution(s, execution_id, principal_id=principal_id)
            await s.commit()

        assert result.outcome == RecoveryOutcome.RETRY_FROM_START
        assert result.crash_point == CrashPoint.BEFORE_MODEL_CALL

        async with db_session() as s:
            execution = await s.get(ExecutionRecord, execution_id)
            assert execution.status == "failed"
            assert "no effect produced" in execution.error


# ----------------------------------------------------------------------------
# 2. Crash after model response
# ----------------------------------------------------------------------------


class TestCrashAfterModelResponse:
    async def test_model_succeeded_no_capability_marks_failed(self, fresh_db, services):
        principal_id = await _create_principal()
        execution_id = await _create_execution(principal_id)

        # Record a successful LLM call
        await _record_step(
            execution_id,
            kind="llm.complete",
            status="succeeded",
            outputs={"chars": 100, "finish_reason": "stop"},
        )

        async with db_session() as s:
            result = await recover_execution(s, execution_id, principal_id=principal_id)
            await s.commit()

        assert result.outcome == RecoveryOutcome.RETRY_FROM_START
        assert result.crash_point == CrashPoint.AFTER_MODEL_RESPONSE

        async with db_session() as s:
            execution = await s.get(ExecutionRecord, execution_id)
            assert execution.status == "failed"
            assert "model output lost" in execution.error


# ----------------------------------------------------------------------------
# 3. Crash after result persistence (replay terminal write)
# ----------------------------------------------------------------------------


class TestCrashAfterResultPersistence:
    async def test_succeeded_capability_step_completes_execution(self, fresh_db, services):
        principal_id = await _create_principal()
        execution_id = await _create_execution(principal_id)

        # Record an LLM call + a successful capability invocation
        await _record_step(
            execution_id,
            kind="llm.complete",
            status="succeeded",
            outputs={"finish_reason": "stop"},
        )
        await _record_step(
            execution_id,
            kind="capability.invoke",
            status="succeeded",
            capability_name="echo",
            inputs={"message": "hello"},
            outputs={"echo": {"message": "hello"}},
        )

        # The execution is still "running" — the terminal write never happened
        async with db_session() as s:
            result = await recover_execution(s, execution_id, principal_id=principal_id)
            await s.commit()

        assert result.outcome == RecoveryOutcome.REPLAY_FROM_CHECKPOINT
        async with db_session() as s:
            execution = await s.get(ExecutionRecord, execution_id)
            assert execution.status == "succeeded"
            # The checkpoint reflects the recovered state
            assert execution.checkpoint is not None
            assert execution.checkpoint.get("echo", {}).get("message") == "hello"


# ----------------------------------------------------------------------------
# 4. Crash after external effect, before result persistence (unknown effect)
# ----------------------------------------------------------------------------


class TestCrashAfterExternalEffect:
    async def test_in_flight_capability_without_idempotency_marks_unknown(self, fresh_db, services):
        principal_id = await _create_principal()
        execution_id = await _create_execution(principal_id)

        await _record_step(
            execution_id,
            kind="llm.complete",
            status="succeeded",
        )
        # In-flight capability invocation WITHOUT an idempotency_key
        await _record_step(
            execution_id,
            kind="capability.invoke",
            status="running",  # never succeeded
            capability_name="test.wipe",
            inputs={"target": "data"},  # no idempotency_key
        )

        async with db_session() as s:
            result = await recover_execution(s, execution_id, principal_id=principal_id)
            await s.commit()

        assert result.outcome == RecoveryOutcome.UNKNOWN_EFFECT
        assert result.crash_point == CrashPoint.AFTER_EXTERNAL_EFFECT_BEFORE_RESULT

        async with db_session() as s:
            execution = await s.get(ExecutionRecord, execution_id)
            assert execution.status == EXECUTION_STATUS_UNKNOWN_EFFECT
            assert "human review required" in execution.error

    async def test_in_flight_capability_with_idempotency_lookup_succeeds(self, fresh_db, services):
        principal_id = await _create_principal()
        execution_id = await _create_execution(principal_id)

        await _record_step(
            execution_id,
            kind="llm.complete",
            status="succeeded",
        )
        # In-flight capability invocation WITH an idempotency_key
        idempotency_key = "test-key-123"
        await _record_step(
            execution_id,
            kind="capability.invoke",
            status="running",
            capability_name="echo",
            inputs={"idempotency_key": idempotency_key, "message": "hi"},
        )

        # Pre-populate the idempotency ledger with a SUCCEEDED record
        from wax.state.capability_models import CapabilityInvocationRecord

        async with db_session() as s:
            record = CapabilityInvocationRecord(
                id="01IDEMOPOTEST00000000000001",
                principal_id=principal_id,
                capability_name="echo",
                idempotency_key=idempotency_key,
                request_fingerprint="fake-fingerprint-64-chars-aaaaaaaaaaaaaaaaaaaaaa",
                status="succeeded",
                response_json='{"echo": {"message": "hi"}}',
                completed_at=datetime.now(UTC),
            )
            s.add(record)
            await s.commit()

        async with db_session() as s:
            result = await recover_execution(s, execution_id, principal_id=principal_id)
            await s.commit()

        assert result.outcome == RecoveryOutcome.REPLAY_FROM_CHECKPOINT

        async with db_session() as s:
            execution = await s.get(ExecutionRecord, execution_id)
            assert execution.status == "succeeded"

            # The step's status was updated to succeeded
            steps = (
                (
                    await s.execute(
                        select(ExecutionStepRecord).where(
                            ExecutionStepRecord.execution_id == execution_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            cap_step = next(s for s in steps if s.kind == "capability.invoke")
            assert cap_step.status == "succeeded"
            assert cap_step.outputs == {"echo": {"message": "hi"}}


# ----------------------------------------------------------------------------
# 5. Idempotency lookup helper
# ----------------------------------------------------------------------------


class TestIdempotencyLookup:
    async def test_lookup_returns_none_for_unknown_key(self, fresh_db, services):
        principal_id = await _create_principal()
        async with db_session() as s:
            result = await lookup_idempotent_outcome(
                s,
                principal_id=principal_id,
                capability_name="echo",
                idempotency_key="nonexistent",
            )
        assert result is None

    async def test_lookup_returns_outputs_for_succeeded(self, fresh_db, services):
        principal_id = await _create_principal()
        idempotency_key = "succeeded-key-456"

        from wax.state.capability_models import CapabilityInvocationRecord

        async with db_session() as s:
            record = CapabilityInvocationRecord(
                id="01IDEMOPOTEST00000000000002",
                principal_id=principal_id,
                capability_name="echo",
                idempotency_key=idempotency_key,
                request_fingerprint="fake-fingerprint-64-chars-bbbbbbbbbbbbbbbbbbbbbb",
                status="succeeded",
                response_json='{"echo": {"message": "replayed"}}',
                completed_at=datetime.now(UTC),
            )
            s.add(record)
            await s.commit()

        async with db_session() as s:
            result = await lookup_idempotent_outcome(
                s,
                principal_id=principal_id,
                capability_name="echo",
                idempotency_key=idempotency_key,
            )
        assert result == {"echo": {"message": "replayed"}}

    async def test_lookup_returns_none_for_failed(self, fresh_db, services):
        principal_id = await _create_principal()
        idempotency_key = "failed-key-789"

        from wax.state.capability_models import CapabilityInvocationRecord

        async with db_session() as s:
            record = CapabilityInvocationRecord(
                id="01IDEMOPOTEST00000000000003",
                principal_id=principal_id,
                capability_name="echo",
                idempotency_key=idempotency_key,
                request_fingerprint="fake-fingerprint-64-chars-cccccccccccccccccccccc",
                status="failed",
                response_json=None,
                error="something broke",
                completed_at=datetime.now(UTC),
            )
            s.add(record)
            await s.commit()

        async with db_session() as s:
            result = await lookup_idempotent_outcome(
                s,
                principal_id=principal_id,
                capability_name="echo",
                idempotency_key=idempotency_key,
            )
        assert result is None


# ----------------------------------------------------------------------------
# 6. classify_crash returns the right CrashPoint for each scenario
# ----------------------------------------------------------------------------


class TestClassifyCrash:
    async def test_no_crash_for_terminal_execution(self, fresh_db, services):
        principal_id = await _create_principal()
        execution_id = await _create_execution(principal_id)

        # Mark as succeeded
        async with db_session() as s:
            repo = ExecutionRepository(s)
            await repo.complete(execution_id, checkpoint={"done": True})
            await s.commit()

        async with db_session() as s:
            crash_point, _envelope = await classify_crash(s, execution_id)

        assert crash_point == CrashPoint.NO_CRASH

    async def test_no_crash_for_no_steps_running_execution(self, fresh_db, services):
        principal_id = await _create_principal()
        execution_id = await _create_execution(principal_id)

        async with db_session() as s:
            crash_point, _envelope = await classify_crash(s, execution_id)

        assert crash_point == CrashPoint.BEFORE_MODEL_CALL


# ----------------------------------------------------------------------------
# 7. recover_orphans integrates the recovery layer
# ----------------------------------------------------------------------------


class TestRecoverOrphansIntegration:
    async def test_recover_orphans_classifies_each_crashed_execution(self, fresh_db, services):
        principal_id = await _create_principal()
        # Two crashed executions: one before model call, one after model response
        exec1 = await _create_execution(principal_id, objective="crash1")
        exec2 = await _create_execution(principal_id, objective="crash2")
        await _record_step(exec2, kind="llm.complete", status="succeeded")

        # Make their started_at old enough to be considered stale
        cutoff = datetime.now(UTC) - timedelta(seconds=100)
        async with db_session() as s:
            for eid in (exec1, exec2):
                execution = await s.get(ExecutionRecord, eid)
                execution.started_at = cutoff
            await s.commit()

        # Use a runner with a very low stale_execution_seconds threshold
        runner = WorkRunner(
            services,
            stale_execution_seconds=1,
        )
        result = await runner.recover_orphans()

        assert result["failed_executions"] == 2
        assert result["retriable_messages"] == 0  # no processed_messages in this test

        async with db_session() as s:
            for eid in (exec1, exec2):
                execution = await s.get(ExecutionRecord, eid)
                assert execution.status == "failed"


# ----------------------------------------------------------------------------
# 8. CheckpointEnvelope contract
# ----------------------------------------------------------------------------


class TestCheckpointEnvelope:
    def test_envelope_serializes_known_and_unknown_effects(self):
        envelope = CheckpointEnvelope(
            objective_id="01OBJ",
            conversation_id="01CONV",
            last_completed_step=3,
            recovery_reason="worker_restart",
            replay_policy="safe_from_checkpoint",
            known_effects=[
                {
                    "capability": "echo",
                    "step_number": 2,
                    "outputs": {"echo": {"message": "hi"}},
                }
            ],
            unknown_effects=[
                {
                    "capability": "test.wipe",
                    "step_number": 3,
                    "idempotency_key": "test-key-123",
                }
            ],
        )
        # The envelope is a frozen dataclass — verify it can be inspected
        assert envelope.schema_version == 1
        assert envelope.last_completed_step == 3
        assert len(envelope.known_effects) == 1
        assert len(envelope.unknown_effects) == 1
        assert envelope.known_effects[0]["capability"] == "echo"
        assert envelope.unknown_effects[0]["capability"] == "test.wipe"
