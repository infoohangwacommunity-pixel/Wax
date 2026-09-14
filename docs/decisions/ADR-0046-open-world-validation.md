# ADR-0046: Open-world Validation

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 13 — Open-world Validation (OMEGA Implementation Directive)

## Context

The directive's success standard:

> The project is architecturally successful when an unfamiliar
> legitimate objective can: establish identity, create an objective,
> preserve evolving memory, survive conversation boundaries, negotiate
> an environment, acquire legitimate tools, execute safely, request
> human authority when needed, recover after interruption, preserve
> evidence, capture artifacts, deliver results, and close only when
> reality proves completion.

Phase 13 proves this by testing WAX against objectives it has never
seen before — composable universal primitives, no domain engines.

## Decision

Write end-to-end acceptance tests that compose the universal runtime
mechanisms for unfamiliar objective patterns:

### Test 1: "Research a topic"

- Objective: "research the history of computing"
- Composes: objective + memory + environment + terminal + artifact
- The intelligence uses terminal.execute to run `echo` commands,
  stores findings as memories, captures the output as an artifact,
  delivers the summary.

### Test 2: "Build software"

- Objective: "build a hello-world program"
- Composes: objective + environment + terminal + workspace + artifact
- The intelligence opens a terminal, writes a file, captures it as
  an artifact.

### Test 3: "Continue when the user replies" (long-running)

- Objective: "remind me to study in 1 second"
- Composes: objective + work + time wake + intelligence re-entry
  (Phase 1) + memory + delivery
- The intelligence schedules a re-entry; the work runner wakes it;
  the continuation creates a memory + delivers a message.

### Test 4: "Monitor for a condition" (event-wake)

- Objective: "tell me when this work finishes"
- Composes: objective + work + event wake + signal ledger +
  intelligence re-entry
- The intelligence schedules a re-entry on `work.succeeded:<id>`;
  when the work finishes, the re-entry fires.

## Tests

`tests/integration/test_open_world_validation.py` covers all 4 patterns.
Each test verifies the full composition works end-to-end through the
real bridge + work runner + capabilities.
