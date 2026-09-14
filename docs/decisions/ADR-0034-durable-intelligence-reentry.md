# ADR-0034: Durable Intelligence Re-entry

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 1 — Durable Intelligence Re-entry (OMEGA Implementation Directive)

## Context

The WAX runtime has durable work (`work.schedule(kind="capability")`) that
wakes at a time or on a runtime signal and invokes a registered capability.
The runtime also has a live intelligence path (`RuntimeBridge._run_intelligence`)
that runs an LLM + tool-call loop in response to an interface message.

**The gap**: a long-running objective that needs the runtime to reason again
after a wake has no generic path. The current work runner can wake and invoke
a *capability* on the AI's behalf, but it cannot wake and re-enter the
*intelligence* itself. Examples that require this:

- "When the user replies, continue this conversation" — wait on
  `interface.message:<principal>`, then wake the intelligence to re-engage.
- "I asked capability X to do work; when it succeeds, re-evaluate the
  objective" — wait on `work.succeeded:<id>`, then wake the intelligence
  to compose the next step.
- "I scheduled a delayed action; when it finishes, decide what to do next"
  — wait on the work ledger, then re-enter reasoning.

Today the only way to "wake and reason" is to wait for the human to send
another interface message. That is the wrong primitive: the runtime owns
time and signals, so the runtime should be able to wake the intelligence
on its own facts, not only on the human's.

## Decision

Add a new durable work kind: `"intelligence"`. When such a work item wakes,
the work runner invokes a **neutral re-entry callback** that the composition
root registers on `RuntimeServices`. The composition root is the SOLE place
where the bridge and the work handler are wired together — the work handler
never imports the bridge, preserving the architecture boundary.

### Work kind

`"intelligence"` — registered alongside `"capability"` in `WorkRunner.register_handler`.
A new `intelligence_handler` runs the re-entry flow.

### Payload schema (bounded)

```json
{
  "prompt": "Reassess the objective after the condition resolved.",
  "observation": {
    "source": "runtime",
    "event": "work.succeeded",
    "work_id": "01M2...",
    "result": {}
  }
}
```

The payload MUST NOT contain secrets, raw tokens, unrestricted artifact
bytes, host paths, authority claims, or unbounded external content. The
handler validates the payload shape and bounds before invoking the
re-entry callback.

### Neutral contracts (in `wax.runtime.work.reentry`)

```python
@dataclass(frozen=True)
class ReentryRequest:
    principal_id: str
    originating_execution_id: str  # the parent execution that scheduled this work
    work_item_id: str             # the work item that woke
    prompt: str                   # bounded intelligence instruction
    observation: dict[str, Any]   # typed runtime evidence about the wake

@dataclass(frozen=True)
class ReentryResult:
    execution_id: str             # the NEW continuation execution
    objective_id: str
    conversation_id: str
    outcome: Literal["succeeded", "failed", "waiting", "awaiting_human"]
    response_text: str | None
    error: str | None
```

The contracts live in `wax.runtime.work.reentry` — a layer the work handler
already imports. The bridge imports the same contracts. Neither imports the
other.

### Composition root wiring

`create_app()` (the composition root) performs the following sequence:

1. Build `RuntimeServices`.
2. Build `RuntimeBridge` (which builds its own intelligence service).
3. Register `bridge.run_reentry` as the re-entry callback on `services`.
4. Build `WorkRunner`, register `capability_handler` for kind `"capability"`
   AND `intelligence_handler` for kind `"intelligence"`.
5. Start the runner.

The `intelligence_handler` reads `services.reentry_callback` and calls it.
If the callback is not registered (e.g. in a stripped-down test setup), the
handler fails honestly: it raises `WorkExecutionError("intelligence re-entry
not configured in this runtime")` and the work retries or dead-letters.

### Wake flow (handler side)

```text
work wakes (time or event)
→ WorkRunner claims the item (lease + fencing)
→ mark_running
→ intelligence_handler(services, item):
    1. Validate payload (prompt ≤ 4000 chars; observation shape; sizes)
    2. Verify principal_id is present
    3. Build ReentryRequest from item.payload + item.principal_id + item.execution_id + item.id
    4. Call services.reentry_callback(request) → ReentryResult
    5. Return result dict to the runner (for mark_succeeded)
```

### Wake flow (bridge side — `bridge.run_reentry(request)`)

```text
1. Resolve the originating objective from execution_id
   (objective_for_execution — uses execution history first)
2. If no objective: return ReentryResult(outcome="failed", error="...")
3. Locate the conversation linked to this objective
4. Verify conversation.principal_id == request.principal_id
   (ownership check — wrong principal is rejected)
5. Create a fresh continuation execution (kind=SINGLE_TURN — continuations
   are still single-turn reasoning cycles against the runtime's evidence)
6. Record objective execution history (kind="work_continuation")
7. Reactivate the objective (sync_active_for_execution)
8. Allocate execution budget
9. Assemble context via ContinuityService (the same composer the bridge uses)
10. Build the LLM message list:
    - System message (same as live path)
    - Conversation history (recent + relevant memory)
    - A RUNTIME OBSERVATION tool message that presents the wake evidence
      as typed data, NOT as user content (so the model cannot impersonate
      the runtime)
11. Run the intelligence + tool-call loop (the SAME loop as live path)
12. Persist continuation memory (episodic: "wake observed: <event>")
13. Persist execution result/checkpoint
14. Record objective execution end (outcome="succeeded" or "failed")
15. Reconcile objective state:
    - If the continuation scheduled more work → objective = waiting
    - If it requested approval → objective = awaiting_human
    - If it is actively reasoning with nothing pending → objective = in_progress
    - Only the intelligence itself can close an objective (via the
      objective.update_status capability if it had evidence) — never auto-close
```

### Why the observation is a tool message, not a user message

The runtime observation (work_id, event, result) is RUNTIME EVIDENCE, not
user input. If it were a user message, the model could be confused into
thinking the human said "the work succeeded" — and might reply as if to a
human. By presenting it as a structured tool response (role=tool,
tool_call_id=runtime-observation), the model sees it as evidence the runtime
produced, and its reply goes back through the same bridge pipeline (with no
recipient, so it goes nowhere — the model is expected to either invoke
capabilities or schedule more work, not chat).

### Scheduling capability

A new runtime capability `intelligence.wait` allows the AI to schedule a
re-entry:

```json
{
  "prompt": "Reassess after the user replies.",
  "wake_event": "interface.message:<principal>",
  "expires_at": "2026-09-22T00:00:00Z"
}
```

OR:

```json
{
  "prompt": "Continue when this work finishes.",
  "wake_event": "work.succeeded:<work_id>"
}
```

The capability schedules a work item with `kind="intelligence"` and
`payload={prompt, observation_template}`. The observation is filled in at
wake time from the actual signal payload.

### Objective completion rule

The re-entry handler MUST NOT auto-close the objective. The intelligence
itself must invoke `objective.update_status` (a separate capability, out of
scope for this ADR) with real evidence to close an objective. The re-entry
handler only reconciles objective state to waiting/awaiting_human/in_progress
based on what the continuation produced.

## Alternatives considered

### Alternative 1: A "wake_callback" on each work kind

Rejected — that would couple the work handler to specific callback types per
kind, defeating the "generic work kinds" architecture. The single
`reentry_callback` slot is cleaner: it's a runtime-owned composition seam.

### Alternative 2: Bridge imports the work handler

Rejected — the architecture boundary tests explicitly forbid
`wax.runtime.work` importing `wax.runtime.bridge`. The bridge is the
interface-agnostic entrypoint; the work handler is a runtime mechanism.
Composition root wires them; neither imports the other.

### Alternative 3: The work handler calls the intelligence service directly

Rejected — the intelligence service is provider-neutral but does NOT know
how to assemble context, resolve the objective, or persist memory. That
logic lives in the bridge. Calling the intelligence service directly would
duplicate the context-assembly + persistence logic, which is exactly the
kind of "duplicate logic" the constitution forbids (Law 6).

### Alternative 4: Add a new ExecutionKind "continuation"

Considered but rejected — `ExecutionKind.SINGLE_TURN` already describes the
shape: one reasoning cycle. A continuation IS a single-turn reasoning cycle
against runtime evidence; the difference is who triggered it (interface vs.
work runner). The execution's `objective` field + the new
`ObjectiveExecutionRecord.kind="work_continuation"` distinguish them. No
schema change needed.

## Consequences

### Positive

- Long-running objectives can wake the intelligence on runtime facts.
- The composition-root pattern keeps the architecture boundary intact.
- The same intelligence loop, context assembly, and tool-call budgeting
  serves both live and re-entry paths.
- The runtime observation is presented as typed evidence, preventing
  prompt-injection where the runtime's fact could be misread as user input.

### Negative

- The bridge now has a second entrypoint (`run_reentry`) alongside `process`.
  The two share `_run_intelligence` but differ in setup (no idempotency
  record for re-entry; the work item itself is the idempotency boundary).
- The `intelligence.wait` capability needs careful scope validation: the
  prompt must be bounded, the wake_event must be a valid signal name, and
  the observation_template must be a closed shape (no `__import__`-style
  "ask the runtime anything" hole).

### Neutral

- The work runner now has two handlers (`capability`, `intelligence`).
  Both are universal mechanisms; neither carries domain logic.

## Security

- The payload is validated: prompt length, observation shape, no secrets.
- The runtime observation is a structured tool message, not user content.
  The model cannot impersonate the runtime by typing into a chat box.
- Ownership: `request.principal_id` is verified against the conversation.
- Lease fencing: the work runner's existing fencing applies — a stale
  worker cannot write a stale continuation result.
- No secrets enter the prompt: the bridge assembles context from memory
  + conversation, neither of which stores secrets (Law 4).
- The re-entry callback is set ONCE at composition root; tests cannot
  accidentally swap it mid-flight.

## Failure semantics

### Missing objective

The originating execution has no objective (e.g. it was deleted). The
handler returns `ReentryResult(outcome="failed", error="no objective for
originating execution")`. The work item is marked failed; if attempts
remain, it retries; otherwise it dead-letters.

### Wrong principal

The conversation linked to the objective belongs to a different principal
than the work item's `principal_id`. This is a security violation (likely
a bug in scheduling). The handler returns `outcome="failed"` with a clear
error; the work item is failed and not retried.

### Intelligence service unavailable

The intelligence service raises during the LLM call. The handler raises
`WorkExecutionError`; the runner applies retry/backoff semantics. The
objective stays in_progress (the wake happened, the continuation
execution failed; the objective does not auto-fail on a single
continuation failure — only on exhaustion of attempts).

### Re-entry callback not registered

`services.reentry_callback is None`. The handler fails honestly:
`WorkExecutionError("intelligence re-entry not configured in this runtime")`.
This is a deployment-configuration error, not a runtime bug.

## Deferred parts

- **`objective.update_status` capability**: the intelligence needs a way to
  close an objective with evidence. This is a separate ADR (Cycle 2
  candidate) — it touches `wax.objective.repository` and the
  `objective.status` transition map.
- **Observation template expansion**: today the observation is filled in
  from the signal payload at wake time. Future cycles may add structured
  observation types (e.g. "memory.changed", "artifact.captured") that the
  intelligence can subscribe to.
- **Continuation chaining limit**: an objective that keeps scheduling
  re-entries indefinitely could exhaust resources. The resource budget
  per execution already bounds this, but a per-objective re-entry counter
  may be needed (Cycle 2 candidate).

## Tests

`tests/integration/test_durable_intelligence_reentry.py` covers:

- time wake invokes intelligence
- event wake invokes intelligence
- runtime observation appears as evidence
- observation is not rendered as system authority
- wrong principal is rejected
- wrong conversation is rejected
- missing objective is rejected
- missing intelligence service retries honestly
- continuation can invoke a capability
- continuation can schedule more work
- continuation can request approval
- continuation can create an artifact
- continuation failure creates execution failure evidence
- work runner retry does not silently duplicate effects
- continuation execution appears in objective history
- credentials never enter the continuation prompt
- lease loss cannot overwrite work state
- objective state is reconciled correctly
