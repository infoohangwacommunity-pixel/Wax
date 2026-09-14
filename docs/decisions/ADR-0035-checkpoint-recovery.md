# ADR-0035: Checkpoint Recovery

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 2 — Checkpoint Recovery (OMEGA Implementation Directive)

## Context

WAX already has execution checkpoints (`ExecutionStepRecord` with status,
inputs, outputs, capability_name) and the repository exposes
`get_latest_succeeded_step` for "resume from the last known good step."
But the runtime does NOT actually use this for crash recovery. Today,
when a worker dies mid-execution:

- The work runner's `recover_orphans` marks the execution as `failed`
  with reason `"runtime restart or crash (recovered by work runner)"`.
- The next attempt starts a FRESH execution — there is no replay of
  the failed execution's completed steps, no idempotency lookup, no
  "unknown external effect" state.

That is honest (the runtime reports the crash honestly), but it is
also crude: an LLM call that succeeded before the crash is repeated
(spend), a capability invocation that succeeded is repeated (a
double-charge against the budget; potentially a double-effect if the
capability is not idempotent).

The mission directive (Phase 2) requires three distinct failure
semantics:

- **retry**: repeat a failed operation (the same execution, the same
  step, again)
- **continuation**: create a fresh execution because a wake condition
  fired (ADR-0034 — already implemented in Phase 1)
- **recovery**: resume a known execution from a safe checkpoint after
  a crash, using recorded idempotency to avoid double-effects

## Decision

Add a recovery layer to the work runner that, when a worker restarts
and finds executions left in `running` state:

1. **Does NOT blindly mark them `failed`** as today. Instead, it
   classifies each crashed execution by the step it reached:

   - `crash_before_model_call`: no `llm.complete` step recorded →
     the execution never produced any effect. Mark `failed` (retryable
     by redelivery of the originating message).
   - `crash_after_model_response`: an `llm.complete` step is succeeded
     but no `capability.invoke` step → the model may have produced a
     tool call the runtime never executed. **Mark `failed` with a
     clear error explaining the model's response was lost.**
     The next attempt starts fresh.
   - `crash_before_capability_invocation`: an `llm.complete` step is
     succeeded, a `capability.invoke` step is pending/running (status
     ≠ succeeded) but no succeeded step exists after it → the
     capability may have been called but its result was lost.
     **Use idempotency lookup**: the capability's `idempotency_key`
     is queried; if the effect succeeded, mark the step `succeeded`
     with the recorded result and continue. If not, mark `failed`
     and retry from the model call.
   - `crash_after_external_effect_before_result_persistence`: the
     capability's effect was issued but the result row never landed.
     Same idempotency-lookup path. If the effect's outcome is
     unknowable, mark the execution's status as `unknown_effect`
     (a new state) and require human review.
   - `crash_after_result_persistence`: the step's outputs are
     recorded. The execution's terminal write failed. **Replay the
     terminal write** (idempotent by construction).

2. **Adds a new execution status `unknown_effect`** to the lifecycle
   state machine: the runtime cannot prove the effect succeeded OR
   failed. The objective's status reflects this honestly (it cannot
   be `succeeded`; it may stay `in_progress` with an audit flag, or
   transition to `failed` after a configurable grace period).

3. **Generic checkpoint envelope** persisted at every durable
   boundary. The envelope contains:
   - `schema_version` (forward-compat)
   - `objective_id` + `conversation_id`
   - `last_completed_step` (step number)
   - `recovery_reason` (`worker_restart`, `lease_expired`, etc.)
   - `replay_policy` (`safe_from_checkpoint`, `from_start`, `unknown`)
   - `known_effects` (list of `{capability, idempotency_key, outcome}`)
   - `unknown_effects` (list of `{capability, idempotency_key}`)

4. **Idempotency lookup helper** in
   `wax.capabilities.idempotency.IdempotencyLedger` — given a
   capability name + idempotency key, returns the recorded outcome
   if it exists. Used by the recovery layer to answer "did this
   capability actually run?"

5. **Recovery tests** at every crash point listed in the directive
   (10 cases), plus tests for the new `unknown_effect` state.

## Alternatives considered

### Alternative 1: Always retry from the start

Rejected — wastes LLM tokens (the model call is repeated), risks
double-effects on non-idempotent capabilities, and the mission
directive explicitly requires distinguishing the three semantics.

### Alternative 2: Mark all crashed executions as `unknown_effect`

Rejected — `unknown_effect` requires human review, which is
expensive. Most crashes happen at well-defined boundaries where the
runtime CAN know what happened. Reserve `unknown_effect` for the
narrow case where the runtime genuinely cannot prove the outcome
(external effect issued, no confirmation, idempotency lookup empty).

### Alternative 3: Store the LLM's full message history in the checkpoint

Considered but deferred — the message history can be large. The
checkpoint envelope stores metadata + idempotency keys, NOT the full
message list. The next attempt's intelligence call rebuilds the
context from continuity (which already includes conversation memory).
This is consistent with ADR-0012 (context is an environment, not a
chat history).

## Consequences

### Positive

- Crashes no longer waste LLM tokens or risk double-effects on
  non-idempotent capabilities.
- The `unknown_effect` state gives the runtime an honest answer when
  it cannot prove an outcome — instead of fabricating success or
  failure.
- The checkpoint envelope is a forward-compatible contract: future
  schema versions can add fields without breaking old recovery logic.

### Negative

- The recovery layer adds complexity to the work runner. The
  classification logic must be tested at every crash point.
- The `unknown_effect` state is terminal-ish — the objective cannot
  auto-recover from it, only the human can. This is intentional
  (honesty > convenience) but may surprise users.

### Neutral

- The idempotency ledger already exists (ADR-0028); this ADR just
  adds a read path for recovery.

## Security

- Recovery does NOT bypass any authority boundary: a recovered
  capability invocation still passes through the same approval
  gate. If the approval was consumed before the crash, it is
  marked consumed; the recovery cannot re-consume it.
- The checkpoint envelope does NOT contain secrets (LLM message
  content is not stored in the envelope; only idempotency keys +
  outcome metadata).
- The `unknown_effect` state is auditable — every transition into
  it produces an audit event with the capability name +
  idempotency key, so a human reviewer can find the in-flight
  external effect.

## Failure semantics

### Capability unavailable during recovery

The capability may have been quarantined or revoked between the
crash and the recovery attempt. The recovery layer treats this as a
normal `denied` outcome — the execution is marked `failed` with a
clear error, NOT `unknown_effect` (the runtime knows the capability
is unavailable; that is a fact, not an unknown).

### Idempotency ledger entry missing

The capability invocation did not record an idempotency key (either
the capability does not support idempotency, or the entry was
pruned). The recovery layer marks the step `unknown_effect` and
requires human review.

### Recovery attempt itself crashes

The recovery is idempotent: running it twice produces the same
outcome (it reads from the same step records + idempotency ledger).

## Deferred parts

- **Replay of LLM context**: the recovery layer does NOT replay the
  exact message history the model saw before the crash. The next
  attempt's intelligence call rebuilds context from continuity
  memory. A future ADR may add "context replay" for cases where the
  intelligence needs to see its own prior reasoning.
- **Multi-step transactions**: today's runtime does not have
  multi-step atomic transactions across capabilities. If a future
  capability composition needs ACID across multiple invocations, a
  separate ADR will add a transaction primitive.

## Tests

`tests/integration/test_checkpoint_recovery.py` covers the 10 crash
points from the directive, plus the `unknown_effect` state and the
idempotency-lookup path.
