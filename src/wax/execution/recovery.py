"""Checkpoint Recovery (ADR-0035, Phase 2).

Distinguishes three failure semantics:

- **retry**: repeat a failed operation (same execution, same step)
- **continuation**: create a fresh execution because a wake fired
  (ADR-0034 — Phase 1)
- **recovery**: resume a known execution from a safe checkpoint,
  using idempotency to avoid double-effects

When a worker restarts and finds executions left in `running` state,
the recovery layer classifies the crash point and decides what to do:

1. `crash_before_model_call` — no `llm.complete` step recorded →
   mark `failed` (retryable by redelivery).
2. `crash_after_model_response` — `llm.complete` succeeded but no
   capability.invoke step → the model's response was lost. Mark
   `failed` with a clear error.
3. `crash_before_capability_invocation` — `capability.invoke` step
   is pending/running but no succeeded step after it → use idempotency
   lookup. If the capability succeeded, mark step succeeded. If not,
   mark execution failed.
4. `crash_after_external_effect_before_result_persistence` — the
   capability's effect was issued but result not recorded. Idempotency
   lookup; if unknown, mark execution `unknown_effect`.
5. `crash_after_result_persistence` — step's outputs recorded but
   execution's terminal write failed. Replay the terminal write
   (idempotent by construction).

The `unknown_effect` state is honest: the runtime cannot prove the
outcome. The objective cannot auto-recover; only the human can.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.execution.repository import ExecutionRepository
from wax.runtime.logging import get_logger
from wax.state.execution_models import ExecutionRecord, ExecutionStepRecord

log = get_logger(__name__)


# New execution status for the "we don't know" case. String-based so
# no migration needed; the executions.status column is String(32).
EXECUTION_STATUS_UNKNOWN_EFFECT = "unknown_effect"


class RecoveryOutcome(StrEnum):
    """What the recovery layer decided for a crashed execution."""

    NO_RECOVERY_NEEDED = "no_recovery_needed"  # execution is not in a crashed state
    RETRY_FROM_START = "retry_from_start"  # mark failed; redelivery will retry
    REPLAY_FROM_CHECKPOINT = "replay_from_checkpoint"  # use recorded idempotency
    UNKNOWN_EFFECT = "unknown_effect"  # cannot prove outcome; needs human review
    ALREADY_TERMINAL = "already_terminal"  # execution already done


class CrashPoint(StrEnum):
    """Where the crash happened relative to the execution's steps."""

    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_RESPONSE = "after_model_response"
    BEFORE_CAPABILITY_INVOCATION = "before_capability_invocation"
    AFTER_EXTERNAL_EFFECT_BEFORE_RESULT = "after_external_effect_before_result"
    AFTER_RESULT_PERSISTENCE = "after_result_persistence"
    NO_CRASH = "no_crash"


@dataclass(frozen=True)
class CheckpointEnvelope:
    """A generic checkpoint envelope persisted at every durable boundary.

    Forward-compatible: `schema_version` lets future runtimes add
    fields without breaking old recovery logic.
    """

    schema_version: int = 1
    objective_id: str | None = None
    conversation_id: str | None = None
    last_completed_step: int = 0
    recovery_reason: str = "worker_restart"
    replay_policy: str = "safe_from_checkpoint"
    known_effects: list[dict[str, Any]] = field(default_factory=list)
    unknown_effects: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RecoveryResult:
    """The outcome of a recovery attempt on one execution."""

    outcome: RecoveryOutcome
    crash_point: CrashPoint
    execution_id: str
    envelope: CheckpointEnvelope | None = None
    error: str | None = None


async def classify_crash(
    session: AsyncSession, execution_id: str
) -> tuple[CrashPoint, CheckpointEnvelope]:
    """Classify where a crashed execution stopped.

    Returns the crash point + a checkpoint envelope with the recorded
    effects (for idempotency lookup).
    """
    execution = await session.get(ExecutionRecord, execution_id)
    if execution is None:
        return CrashPoint.NO_CRASH, CheckpointEnvelope()

    if execution.status not in ("running", "pending"):
        return CrashPoint.NO_CRASH, CheckpointEnvelope()

    # Read all steps in order
    steps = (
        await session.execute(
            select(ExecutionStepRecord)
            .where(ExecutionStepRecord.execution_id == execution_id)
            .order_by(ExecutionStepRecord.step_number.asc())
        )
    ).scalars().all()

    if not steps:
        objective_id = await _resolve_objective_id(session, execution_id)
        return CrashPoint.BEFORE_MODEL_CALL, CheckpointEnvelope(
            objective_id=objective_id,
            last_completed_step=0,
        )

    # Walk the steps and find the last succeeded step
    last_succeeded_step = 0
    capability_steps: list[ExecutionStepRecord] = []
    for step in steps:
        if step.status == "succeeded":
            last_succeeded_step = step.step_number
        if step.kind == "capability.invoke":
            capability_steps.append(step)

    # Build the envelope
    known_effects: list[dict[str, Any]] = []
    unknown_effects: list[dict[str, Any]] = []
    for cs in capability_steps:
        if cs.status == "succeeded":
            known_effects.append(
                {
                    "capability": cs.capability_name,
                    "step_number": cs.step_number,
                    "outputs": cs.outputs,
                }
            )
        elif cs.status in ("pending", "running"):
            # In-flight capability invocation — idempotency lookup needed
            idempotency_key = (cs.inputs or {}).get("idempotency_key")
            if idempotency_key:
                unknown_effects.append(
                    {
                        "capability": cs.capability_name,
                        "step_number": cs.step_number,
                        "idempotency_key": idempotency_key,
                    }
                )
            else:
                # No idempotency key recorded — cannot prove outcome
                unknown_effects.append(
                    {
                        "capability": cs.capability_name,
                        "step_number": cs.step_number,
                        "idempotency_key": None,
                    }
                )

    objective_id = await _resolve_objective_id(session, execution_id)
    envelope = CheckpointEnvelope(
        objective_id=objective_id,
        last_completed_step=last_succeeded_step,
        known_effects=known_effects,
        unknown_effects=unknown_effects,
    )

    # Classify the crash point
    if last_succeeded_step == 0:
        return CrashPoint.BEFORE_MODEL_CALL, envelope

    # Did the model produce a tool call that didn't complete?
    last_step = steps[-1]
    if last_step.kind == "llm.complete" and last_step.status == "succeeded":
        # The model call succeeded; was there a capability.invoke after?
        has_capability_after = any(
            s.kind == "capability.invoke" for s in steps
        )
        if not has_capability_after:
            return CrashPoint.AFTER_MODEL_RESPONSE, envelope

    # Was there a capability.invoke that didn't succeed?
    pending_capability = any(
        cs.status in ("pending", "running") for cs in capability_steps
    )
    if pending_capability:
        return CrashPoint.AFTER_EXTERNAL_EFFECT_BEFORE_RESULT, envelope

    # Was the last step a succeeded capability.invoke with no terminal
    # write on the execution itself?
    if (
        last_step.kind == "capability.invoke"
        and last_step.status == "succeeded"
        and execution.status == "running"
    ):
        return CrashPoint.AFTER_RESULT_PERSISTENCE, envelope

    # Default: the model call was the last step (no tool calls)
    return CrashPoint.AFTER_MODEL_RESPONSE, envelope


async def _resolve_objective_id(
    session: AsyncSession, execution_id: str
) -> str | None:
    """Resolve the objective an execution is working on (best-effort)."""
    from wax.objective.evidence import objective_for_execution

    objective = await objective_for_execution(session, execution_id)
    return objective.id if objective else None


async def lookup_idempotent_outcome(
    session: AsyncSession,
    *,
    principal_id: str,
    capability_name: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    """Look up the recorded outcome of an idempotent capability invocation.

    Returns the outputs dict if the invocation succeeded, None otherwise.
    Used by the recovery layer to answer "did this capability actually
    run?" without re-executing it.
    """
    from wax.state.capability_models import CapabilityInvocationRecord

    record = (
        await session.execute(
            select(CapabilityInvocationRecord)
            .where(CapabilityInvocationRecord.principal_id == principal_id)
            .where(
                CapabilityInvocationRecord.capability_name == capability_name[:255]
            )
            .where(
                CapabilityInvocationRecord.idempotency_key == idempotency_key[:512]
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    if record.status != "succeeded":
        return None
    try:
        return json.loads(record.response_json) if record.response_json else None
    except (json.JSONDecodeError, TypeError):
        return None


async def recover_execution(
    session: AsyncSession,
    execution_id: str,
    *,
    principal_id: str | None = None,
) -> RecoveryResult:
    """Recover a crashed execution by classifying the crash point and
    applying the appropriate recovery semantics.

    This is the entrypoint called by the work runner's recover_orphans.
    """
    execution = await session.get(ExecutionRecord, execution_id)
    if execution is None:
        return RecoveryResult(
            outcome=RecoveryOutcome.NO_RECOVERY_NEEDED,
            crash_point=CrashPoint.NO_CRASH,
            execution_id=execution_id,
            error="execution not found",
        )

    if execution.status not in ("running", "pending"):
        return RecoveryResult(
            outcome=RecoveryOutcome.ALREADY_TERMINAL,
            crash_point=CrashPoint.NO_CRASH,
            execution_id=execution_id,
        )

    crash_point, envelope = await classify_crash(session, execution_id)
    exec_repo = ExecutionRepository(session)
    now = datetime.now(UTC)

    if crash_point == CrashPoint.BEFORE_MODEL_CALL:
        # No effect was produced. Mark failed (retryable).
        await exec_repo.fail(
            execution_id,
            error="crash before model call (recovered; no effect produced)",
        )
        log.info(
            "execution.recovered",
            execution_id=execution_id,
            crash_point=crash_point.value,
            outcome=RecoveryOutcome.RETRY_FROM_START.value,
        )
        return RecoveryResult(
            outcome=RecoveryOutcome.RETRY_FROM_START,
            crash_point=crash_point,
            execution_id=execution_id,
            envelope=envelope,
        )

    if crash_point == CrashPoint.AFTER_MODEL_RESPONSE:
        # The model call succeeded but its response is lost. Cannot replay
        # the model safely (we don't have the message history). Mark failed.
        await exec_repo.fail(
            execution_id,
            error="crash after model response (recovered; model output lost)",
        )
        log.info(
            "execution.recovered",
            execution_id=execution_id,
            crash_point=crash_point.value,
            outcome=RecoveryOutcome.RETRY_FROM_START.value,
        )
        return RecoveryResult(
            outcome=RecoveryOutcome.RETRY_FROM_START,
            crash_point=crash_point,
            execution_id=execution_id,
            envelope=envelope,
        )

    if crash_point == CrashPoint.AFTER_RESULT_PERSISTENCE:
        # The last step's outputs are recorded but the execution's
        # terminal write failed. Replay the terminal write.
        last_step = (
            await session.execute(
                select(ExecutionStepRecord)
                .where(ExecutionStepRecord.execution_id == execution_id)
                .order_by(ExecutionStepRecord.step_number.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last_step and last_step.outputs:
            await exec_repo.complete(
                execution_id,
                checkpoint=last_step.outputs,
            )
            log.info(
                "execution.recovered",
                execution_id=execution_id,
                crash_point=crash_point.value,
                outcome=RecoveryOutcome.REPLAY_FROM_CHECKPOINT.value,
            )
            return RecoveryResult(
                outcome=RecoveryOutcome.REPLAY_FROM_CHECKPOINT,
                crash_point=crash_point,
                execution_id=execution_id,
                envelope=envelope,
            )

    if crash_point == CrashPoint.AFTER_EXTERNAL_EFFECT_BEFORE_RESULT:
        # In-flight capability invocation — use idempotency lookup.
        if not envelope.unknown_effects:
            # No in-flight capability to recover — fall through to failed
            await exec_repo.fail(
                execution_id,
                error="crash with no recoverable effects (recovered)",
            )
            return RecoveryResult(
                outcome=RecoveryOutcome.RETRY_FROM_START,
                crash_point=crash_point,
                execution_id=execution_id,
                envelope=envelope,
            )

        # Try idempotency lookup for each unknown effect
        any_unknown = False
        for unknown in envelope.unknown_effects:
            capability_name = unknown.get("capability")
            idempotency_key = unknown.get("idempotency_key")
            if not capability_name or not idempotency_key:
                # No idempotency key — cannot prove outcome
                any_unknown = True
                continue

            # Look up the recorded outcome
            recorded = await lookup_idempotent_outcome(
                session,
                principal_id=principal_id or execution.principal_id,
                capability_name=capability_name,
                idempotency_key=idempotency_key,
            )
            if recorded is None:
                # The capability's outcome is unknowable
                any_unknown = True
                continue

            # Mark the step as succeeded with the recorded outputs
            step = (
                await session.execute(
                    select(ExecutionStepRecord)
                    .where(
                        ExecutionStepRecord.execution_id == execution_id,
                        ExecutionStepRecord.step_number == unknown.get("step_number"),
                    )
                )
            ).scalar_one_or_none()
            if step is not None:
                step.status = "succeeded"
                step.outputs = recorded
                step.error = None

        if any_unknown:
            # Mark the execution with the new unknown_effect status
            execution.status = EXECUTION_STATUS_UNKNOWN_EFFECT
            execution.ended_at = now
            execution.error = (
                "crash with unknown external effect (idempotency lookup "
                "could not prove outcome for one or more in-flight "
                "capability invocations; human review required)"
            )
            await session.flush()
            log.warning(
                "execution.recovered.unknown_effect",
                execution_id=execution_id,
                crash_point=crash_point.value,
            )
            return RecoveryResult(
                outcome=RecoveryOutcome.UNKNOWN_EFFECT,
                crash_point=crash_point,
                execution_id=execution_id,
                envelope=envelope,
                error=execution.error,
            )

        # All in-flight effects were proven successful — complete the execution
        await exec_repo.complete(
            execution_id,
            checkpoint={
                "recovered": True,
                "crash_point": crash_point.value,
            },
        )
        log.info(
            "execution.recovered",
            execution_id=execution_id,
            crash_point=crash_point.value,
            outcome=RecoveryOutcome.REPLAY_FROM_CHECKPOINT.value,
        )
        return RecoveryResult(
            outcome=RecoveryOutcome.REPLAY_FROM_CHECKPOINT,
            crash_point=crash_point,
            execution_id=execution_id,
            envelope=envelope,
        )

    # Fallback: mark failed honestly
    await exec_repo.fail(
        execution_id,
        error=f"unclassified crash (crash_point={crash_point.value})",
    )
    return RecoveryResult(
        outcome=RecoveryOutcome.RETRY_FROM_START,
        crash_point=crash_point,
        execution_id=execution_id,
        envelope=envelope,
    )
