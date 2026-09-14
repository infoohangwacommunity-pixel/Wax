# ADR-0037: Context Becomes Environment

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 4 — Context Becomes Environment (OMEGA Implementation Directive)

## Context

The ContinuityService already composed most of the context the AI needs:
conversation, recent memory, active objective, last execution, active
work, recent artifacts, interface. But the directive (Phase 4) requires
the AI to "wake inside reality, not chat history" — the environment
must also carry:

- The runtime's clock (current_time)
- The set of available capabilities (so the AI can compose what's
  available without a hardcoded list)
- Recent signals (so the AI sees what woke up)
- The count of waiting work (distinct from active work — items in
  `waiting` status specifically)

## Decision

Add a `_build_environment` helper to `ContinuityService` that composes
these environment facts. The helper is **tolerant** — any subsystem
that cannot be queried contributes nothing to the dict, never raises.
This is the "degrade gracefully" requirement.

The environment dict now carries:

- `interface`: which interface this interaction is on (already present)
- `current_time`: the runtime's clock, ISO-8601 with timezone
- `available_capabilities`: list of `{name, description, is_destructive}`
  for each registered capability (when the session has access to the
  RuntimeServices container)
- `recent_signals`: the last 5 runtime signals emitted for this
  principal (so the AI sees what woke up — work.succeeded,
  approval.granted, etc.)
- `waiting_work_count`: how many durable work items are in `waiting`
  status specifically (distinct from `active_work` which carries all
  non-terminal items)
- `active_work_count`: how many active work items (already present)

## Alternatives considered

### Alternative 1: Bake the environment into the system prompt

Rejected — the system prompt is a static string. The environment is
dynamic (capabilities change as new ones are registered; signals
arrive; the clock moves). Baking them into the prompt would either
require prompt reconstruction on every call (which is what we're doing
via the evidence assembly anyway) or staleness.

### Alternative 2: Add a separate `EnvironmentService`

Considered but rejected — the ContinuityService is already the single
composer. Adding another service would duplicate the composition logic
and create a coordination seam (which service builds what part of
the context?).

## Consequences

### Positive

- The AI now sees the runtime's clock — it can reason about time
  ("how long has this work been waiting?", "when does this approval
  expire?")
- The AI sees the available capabilities — it can compose what's
  available without a hardcoded list. New capabilities registered at
  runtime appear in the next context build.
- The AI sees recent signals — it can react to "work.succeeded" or
  "approval.granted" events that just happened.
- Degrades gracefully: a session without the services container
  (e.g. unit tests) still gets the basics (interface + current_time).

### Negative

- The environment dict is larger. Evidence assembly must respect the
  character budget (it already does — `assemble_evidence` truncates
  within the budget).

### Neutral

- The `_build_environment` helper is tolerant — wrapped in
  try/except for each subsystem. This is intentional: a failure to
  read signals must not break context assembly.

## Security

- The environment dict contains NO secrets. The available_capabilities
  list has names + descriptions (already public information). The
  recent_signals list has names + emitted_at + emitted_by (already
  audit-visible). No capability inputs/outputs are included.
- The `wax_services` attribute on the session is set by the bridge's
  composition root — tests cannot accidentally leak a different
  services container (the attribute is not serialized, not logged).

## Tests

`tests/integration/test_context_environment.py` covers:

- current_time present in environment
- interface present
- available_capabilities present when services attached
- recent_signals present after signal emission
- waiting_work_count is 0 when no work is waiting
- degrades gracefully without services container
- active objective surfaced (priority item)
