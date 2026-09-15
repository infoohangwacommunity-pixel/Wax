# ADR-0044: Multi-instance Runtime

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 11 — Multi-instance Runtime (OMEGA Implementation Directive)

## Context

WAX's lease design already admits replicas: `WorkRunner.claim_due` uses
`FOR UPDATE SKIP LOCKED` on PostgreSQL (SQLite fallback: single-writer
locking), and `wax.runtime.leadership` has advisory-lock leader election.
But several mechanisms are still process-local:

- Rate limits (`RateLimiter`) — in-memory counters per process
- Cost caps (`CostProtector`) — in-memory per-principal totals
- Abuse counters (`AbuseDetector`) — in-memory
- Provider breaker state (`ResilientProvider`) — in-memory circuit state

A two-process deployment would have each process maintain its own
counters, doubling the effective rate limit and cost cap.

## Decision

### 1. Document the shared-state boundary

Add `wax.runtime.shared_state` module that declares which mechanisms
MUST be shared across processes for multi-instance correctness, and
which are legitimately process-local.

**Must be shared (process-local is a correctness bug in multi-instance)**:
- Rate limit counters
- Cost cap totals
- Abuse detection verdicts
- Execution lease ownership (already DB-backed — OK)
- Work item lease fencing (already DB-backed — OK)
- Approval consumption (already DB-backed — OK)
- Idempotency ledger (already DB-backed — OK)

**Legitimately process-local**:
- Provider circuit breaker state (each process can independently
  observe provider failures; a circuit open in one process doesn't
  need to be open in another — the retry/circuit-breaker semantics
  are per-process)
- In-flight asyncio task tracking (process-local by definition)
- The mock LLM provider's script (test-only)

### 2. DB-backed rate limiter

Add a `DbBackedRateLimiter` that stores counters in a new
`rate_limit_counters` table. Each process reads + writes atomically
(via conditional UPDATE). Falls back to the in-memory `RateLimiter`
when the DB is not available (single-process dev mode).

### 3. DB-backed cost protector

Add a `DbBackedCostProtector` that stores per-principal cost totals
in a new `cost_tracking` table. Same atomic UPDATE pattern.

### 4. Multi-worker concurrency test

A test that creates two `WorkRunner` instances (simulating two
processes) and verifies they never claim the same work item.

## Alternatives considered

### Alternative 1: Redis-backed shared state

Considered — Redis is the standard for shared rate limits. But
adding Redis as a dependency violates the "no external services
required" principle. The DB-backed approach works with the existing
PostgreSQL deployment.

### Alternative 2: Just document the limitation

Rejected — the directive explicitly requires "shared correctness"
validation. A process-local rate limiter in a two-process deployment
is a real bug.

## Tests

- Two WorkRunners never claim the same item (simulated multi-process)
- DB-backed rate limiter enforces across "processes" (simulated)
- DB-backed cost protector enforces across "processes" (simulated)
