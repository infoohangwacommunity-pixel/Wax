# ADR-0027: Multi-Server State — What Is Durable, What Is Local, What Is Honestly Broken

Date: 2026-09-14
Status: Accepted (classification authoritative; two gaps fixed this cycle; two boundaries documented)
Context: OMEGA mission §61 (multi-server state), §44 (isolation honesty).

## Context

The repo's deployment documentation claims "each instance is stateless
(state lives in PostgreSQL)". The §61 audit traced every piece of
process-local state in the runtime and classified it. The claim holds
for business state and does not hold for enforcement state. Full
classification (37 items, verified at `10c2333`):

**REQUIRED TO BE DURABLE — already durable (8):** work-item lease/wake
state (`FOR UPDATE SKIP LOCKED` + fencing, ADR-0013), runtime signal
ledger, delivery records with retry chain, processed-message
idempotency, approvals (one-time conditional-UPDATE consumption),
audit events, all principal-scoped state models, provisioning rows.

**REQUIRED TO BE SHARED — implemented (3):** work leases/fencing;
maintenance advisory-lock leadership (ADR-0017); approval exactly-once
consumption. This cycle added three more (below).

**REQUIRED TO BE SHARED — gaps fixed this cycle (3):**
1. The provisioning TTL reaper destroys real filesystem directories;
   it ran on EVERY replica with no lease, no row lock, no leadership.
   Now leadership-guarded (the ADR-0017 advisory-lock pattern).
2. The memory expiry loop ran unguarded on every replica (idempotent
   but multiplied audit rows and contention). Now leadership-guarded.
3. The conversation lifecycle sweep (newly wired) runs inside the
   leader-only maintenance pass, so it inherits the guard.

**INTENTIONALLY LOCAL (16):** the ASGI app, `app.state` container,
`RuntimeServices` container, lifecycle manager, worker identity (the
fencing token IS per-process), handler registry (shared by
configuration — every replica wires identical handlers), runner task
flags, delivery router wiring, engine/pool singletons, capability
registry (identical static build per process; durable work references
capability NAMES, not objects), invoker, provider failover chain,
httpx pools, mock provider (test seam), read-only module constants.

**SAFELY DISPOSABLE (7):** metrics registries (standard per-instance
Prometheus model; `work_items_inflight` is fleet-true — read from the
DB), circuit-breaker state (crash resets it; correctness preserved,
efficiency lost), resilient-provider metric edge detector, the
content-addressed artifact cache (verify-on-read, atomic renames,
multi-process-safe by construction), media temp files.

## Decision

**1. The stateless claim is restated honestly.** Business state
(identity, memory, objectives, work, deliveries, approvals, audit) is
multi-server correct and DB-backed. Enforcement state is NOT, and the
deployment documentation must not claim otherwise.

**2. The security perimeter is single-instance today — documented as a
boundary, not silently broken.** `RateLimiter._buckets`,
`CostProtector._usage`, `AbuseDetector._message_times` are in-memory
dicts that multiply by replica count under multi-server deployment and
reset on restart (the code itself says "for production, use Redis").
Decision: keep them in-memory (no external dependency is justified by
current deployment reality), require single-instance deployment OR a
shared enforcement store before scaling out, and treat a DB/Redis-
backed enforcement perimeter as the designed future primitive. Running
N replicas today silently multiplies every rate/cost/abuse limit by N —
an operator must know this.

**3. Two uncovered races are documented with their designed fixes:**
- `recover_orphans()` runs at every replica startup and marks running
  executions older than 900s as failed — a rolling multi-instance
  deploy can kill another replica's live long execution (bridge
  executions carry no lease). Designed fix: an execution heartbeat
  (lease column + periodic renewal) so orphan recovery checks
  LIVENESS, not age. Deferred: single long-execution semantics are not
  yet load-bearing; when multi-instance deployments become real, this
  lands first.
- Per-execution resource budgets live in the process-local
  accountant; lease reclaim re-allocates a fresh budget (at-least-once
  semantics reset spend per attempt, bounded by `max_attempts`), and
  cross-process consumption is denied-by-default (refused, not
  overspent). Acceptable under single-instance + leader-guarded
  destructive sweeps; a distributed path would need the budget in the
  same shared store as the enforcement perimeter.

**4. Multi-server rule going forward:** any new state must be
classified into this ADR's table at review time. Anything affecting
correctness across replicas must be DB-backed and, where destructive,
leadership-guarded — the runtime already owns the patterns; the rule
is that they are mandatory, not optional hygiene.

## Consequences

- Three destructive/lifecycle sweeps became leader-only this cycle; the
  deployment story for the durable core (leases, fencing, approvals,
  idempotency, deliveries) is verified true.
- The honest deployment statement is: "single-instance for the
  enforcement perimeter; the durable-work core is already
  multi-instance correct; two designed primitives (execution
  heartbeat, shared enforcement store) are recorded here and land
  before real scale-out."
