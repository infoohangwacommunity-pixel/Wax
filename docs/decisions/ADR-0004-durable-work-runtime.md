# ADR-0004: Durable Work Runtime — Work That Survives Time

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX implementation agent, per roadmap Phase R/V

## Context

"Remind me in one hour" must not be built as a ReminderService. WAX's
philosophy: the runtime provides **mechanisms** — retaining work, waiting,
waking, resuming, delivering, cleaning up — and the AI composes them into
whatever the user means. The audit found no mechanism by which any work
could outlive the process that started it.

## Decision

The runtime owns a **work item** abstraction with these properties:

- **Persisted** in Postgres (`work_items`) before it is first attempted;
  a crash after enqueue loses nothing.
- **Wake conditions**, not cron entries: a work item names the condition
  that makes it runnable (time-at, event-match, dependency-completion).
- **Leases**: a worker claims an item by lease; a crashed worker's lease
  expires and another worker resumes. Work is never lost to a dead owner.
- **Idempotency keys**: re-execution after failure is safe; the item
  records attempts and cannot double-deliver.
- **Retry with backoff**, then **dead-letter**: terminal failures are
  quarantined with full context for AI inspection, never silently dropped.
- **Identity continuity**: a work item carries the principal that created
  it, so delayed output reaches the user through the same identity — the
  AI's promise survives the AI's process.

The worker loop runs in-process (lifespan-started) for this deployment
stage; the lease design permits multi-process without change.

## What this deliberately is NOT

- Not a scheduler API for AI code to call ad hoc (`set_timeout`).
- Not a domain concept (no "reminder", "timer", "study session" types).
  A reminder is: create work item → wake at T → invoke message.send
  capability → through Agency if required → deliver → clean up.

## Consequences

- Phase V (background runtime) composes on this base rather than inventing
  a second mechanism.
- Time is the runtime's; decisions are the AI's. The AI cannot fake success:
  if delivery fails, the item dead-letters with the real error.
