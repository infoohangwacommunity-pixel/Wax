"""Execution recovery — clean rewrite (Phase I, Part 22/48).

No capabilities. No objectives. No old idempotency assumptions.

The recovery model is now built on:
- Checkpoints: the intelligence loop state (message history) persisted
  after each terminal round
- Observations: terminal execution results recorded as steps
- Effect uncertainty: terminal commands may have had external effects
  we can't prove (we don't know if the email was sent, if the API call
  succeeded, etc.)
- Reconciliation: on restart, classify the crash point and decide
  whether to resume, retry, or mark unknown

The terminal is the only effect mechanism. There is no capability
invocation, no idempotency ledger, no objective resolution. The
recovery layer reasons about: did the intelligence loop checkpoint
exist? Was the last terminal observation recorded? Was there a
terminal command whose effect we can't verify?

Recovery outcomes:
- RESUME_FROM_CHECKPOINT: a terminal-loop checkpoint exists; resume
  the intelligence loop with the same message history
- RETRY_FROM_START: no checkpoint or checkpoint is stale; mark failed
  so redelivery retries
- UNKNOWN_EFFECT: a terminal command ran but we can't prove its
  outcome; needs human review
- ALREADY_TERMINAL: execution already completed
- NO_RECOVERY_NEEDED: execution is not in a crashed state
"""

from __future__ import annotations

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


class RecoveryOutcome(StrEnum):
    """What the recovery layer decided for a crashed execution."""

    NO_RECOVERY_NEEDED = "no_recovery_needed"
    RESUME_FROM_CHECKPOINT = "resume_from_checkpoint"
    RETRY_FROM_START = "retry_from_start"
    UNKNOWN_EFFECT = "unknown_effect"
    ALREADY_TERMINAL = "already_terminal"


class CrashPoint(StrEnum):
    """Where the crash happened relative to the execution's steps."""

    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_RESPONSE = "after_model_response"
    AFTER_TERMINAL_EXECUTION = "after_terminal_execution"
    AFTER_RESULT_PERSISTENCE = "after_result_persistence"
    NO_CRASH = "no_crash"


@dataclass(frozen=True)
class CheckpointEnvelope:
    """A checkpoint envelope persisted at every durable boundary."""

    schema_version: int = 2
    last_completed_step: int = 0
    recovery_reason: str = "worker_restart"
    has_terminal_checkpoint: bool = False
    terminal_round: int = 0
    message_count: int = 0
    pending_terminal_effects: list[dict[str, Any]] = field(default_factory=list)


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

    Walks the execution steps + checkpoint to determine the crash point.
    """
    execution = await session.get(ExecutionRecord, execution_id)
    if execution is None:
        return CrashPoint.NO_CRASH, CheckpointEnvelope()

    if execution.status not in ("running", "pending"):
        return CrashPoint.NO_CRASH, CheckpointEnvelope()

    # Check for a terminal-loop checkpoint (the intelligence loop state)
    has_terminal_checkpoint = (
        isinstance(execution.checkpoint, dict) and "messages" in execution.checkpoint
    )
    terminal_round = 0
    message_count = 0
    if has_terminal_checkpoint:
        terminal_round = int(execution.checkpoint.get("round", 0))
        message_count = len(execution.checkpoint.get("messages", []))

    # Read all steps in order
    steps = (
        (
            await session.execute(
                select(ExecutionStepRecord)
                .where(ExecutionStepRecord.execution_id == execution_id)
                .order_by(ExecutionStepRecord.step_number.asc())
            )
        )
        .scalars()
        .all()
    )

    if not steps:
        return CrashPoint.BEFORE_MODEL_CALL, CheckpointEnvelope(
            last_completed_step=0,
            has_terminal_checkpoint=has_terminal_checkpoint,
            terminal_round=terminal_round,
            message_count=message_count,
        )

    # Walk the steps and find the last succeeded step
    last_succeeded_step = 0
    terminal_steps: list[ExecutionStepRecord] = []
    pending_effects: list[dict[str, Any]] = []

    for step in steps:
        if step.status == "succeeded":
            last_succeeded_step = step.step_number
        if step.kind == "terminal":
            terminal_steps.append(step)
            # Check if this terminal step has an observation recorded
            if step.status in ("pending", "running"):
                # Terminal command was issued but no observation —
                # we don't know if it succeeded
                pending_effects.append(
                    {
                        "step_number": step.step_number,
                        "command": (step.inputs or {}).get("command", "")[:200],
                        "status": step.status,
                    }
                )

    envelope = CheckpointEnvelope(
        last_completed_step=last_succeeded_step,
        has_terminal_checkpoint=has_terminal_checkpoint,
        terminal_round=terminal_round,
        message_count=message_count,
        pending_terminal_effects=pending_effects,
    )

    # Classify the crash point
    if pending_effects:
        # A terminal command ran but we can't prove its outcome
        return CrashPoint.AFTER_TERMINAL_EXECUTION, envelope

    # Check if the last step was a terminal observation
    last_step = steps[-1]
    if last_step.kind == "terminal_observation" and last_step.status == "succeeded":
        return CrashPoint.AFTER_RESULT_PERSISTENCE, envelope

    if last_step.kind == "model_response" and last_step.status == "succeeded":
        return CrashPoint.AFTER_MODEL_RESPONSE, envelope

    return CrashPoint.BEFORE_MODEL_CALL, envelope


async def recover_execution(
    session: AsyncSession,
    execution_id: str,
    *,
    principal_id: str | None = None,
) -> RecoveryResult:
    """Recover a crashed execution.

    Classification logic:
    - If a terminal-loop checkpoint exists → RESUME_FROM_CHECKPOINT
      (the bridge's resume_execution will be called by the work runner)
    - If pending terminal effects exist (command ran, no observation) →
      UNKNOWN_EFFECT (human review needed)
    - If the last step was a terminal observation → complete the execution
      (the result was persisted, just the terminal write failed)
    - If the model call succeeded but no terminal followed → RETRY_FROM_START
    - If no steps ran → RETRY_FROM_START (no effect produced)
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

    # If we have a terminal-loop checkpoint, the work runner will handle
    # the resume via resume_callback. Mark the recovery outcome but don't
    # change the execution state — the runner will call resume_execution.
    if envelope.has_terminal_checkpoint:
        log.info(
            "execution.recovered.checkpoint_exists",
            execution_id=execution_id,
            crash_point=crash_point.value,
            terminal_round=envelope.terminal_round,
        )
        return RecoveryResult(
            outcome=RecoveryOutcome.RESUME_FROM_CHECKPOINT,
            crash_point=crash_point,
            execution_id=execution_id,
            envelope=envelope,
        )

    # Pending terminal effects — we can't prove the outcome
    if envelope.pending_terminal_effects:
        execution.status = "unknown_effect"
        execution.ended_at = datetime.now(UTC)
        execution.error = (
            "crash with unverified terminal effect — the AI ran a command "
            "but its outcome could not be confirmed after restart; human "
            "review required"
        )
        await session.flush()
        log.warning(
            "execution.recovered.unknown_effect",
            execution_id=execution_id,
            pending_effects=len(envelope.pending_terminal_effects),
        )
        return RecoveryResult(
            outcome=RecoveryOutcome.UNKNOWN_EFFECT,
            crash_point=crash_point,
            execution_id=execution_id,
            envelope=envelope,
            error=execution.error,
        )

    # Last step was a terminal observation — the result was persisted,
    # just the terminal write (execution.complete) failed. Complete it.
    if crash_point == CrashPoint.AFTER_RESULT_PERSISTENCE:
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
                checkpoint={"recovered": True, "crash_point": crash_point.value},
            )
            log.info(
                "execution.recovered.completed",
                execution_id=execution_id,
                crash_point=crash_point.value,
            )
            return RecoveryResult(
                outcome=RecoveryOutcome.RESUME_FROM_CHECKPOINT,
                crash_point=crash_point,
                execution_id=execution_id,
                envelope=envelope,
            )

    # Model response was lost or no steps ran — retry from start
    await exec_repo.fail(
        execution_id,
        error=f"crash at {crash_point.value} (recovered; no verifiable effect produced)",
    )
    log.info(
        "execution.recovered.retry",
        execution_id=execution_id,
        crash_point=crash_point.value,
    )
    return RecoveryResult(
        outcome=RecoveryOutcome.RETRY_FROM_START,
        crash_point=crash_point,
        execution_id=execution_id,
        envelope=envelope,
    )
