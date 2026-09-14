# ADR-0013: Multi-Worker Durable Runtime — Fencing, Heartbeats, Loud Deaths

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX principal-engineer continuation session (fourth pass)

## Context

The work runner was a single in-process loop. The lease design admitted
replicas on paper, but four correctness mechanisms were missing — and a
"deployment-stage" simplification must never be an excuse for a known
correctness boundary:

1. **Claiming was not atomic across processes.** `claim_due` SELECTed rows
   then mutated them in-transaction; two concurrent Postgres workers could
   read the same rows before either committed and both would run the item.
2. **A zombie worker could write.** A worker whose lease expired and whose
   item was reclaimed could still `mark_succeeded` its stale result,
   overwriting the authoritative attempt.
3. **A healthy slow worker was indistinguishable from a dead one.** A
   legitimate long execution (bounded at 300s) exceeded the 120s lease;
   without renewal it was reclaimed mid-run — double execution by design.
4. **Some deaths were silent.** A reclaim that exhausted attempts died
   without a dead-letter row or `work.dead` signal; an expired wait died
   without any announcement — dependents had no honest fact to react to.

## Research

- **Fencing tokens (Kleinberg & Robinson; Chubby/ZooKeeper lease
  practice)** — a lease holder that loses its lease must be refused at
  WRITE time, not trusted at read time. WAX's fencing token is the lease
  ownership itself: terminal writes take `expected_owner` and compare.
- **Postgres SKIP LOCKED queue patterns** — the canonical multi-worker
  claim: `SELECT … FOR UPDATE SKIP LOCKED` makes the claim a single
  atomic step; workers never block each other or double-claim.
- **SQS visibility timeouts / heartbeats** — a worker renews its lease
  while healthy (SQS heartbeat extension); only an unrenewed lease
  signals death. Renewal interval = lease/3, so two failed renewals
  still leave a live lease.
- **At-least-once semantics (Temporal/queue systems)** — an attempt's
  effects may repeat after a crash; the runtime's duty is (a) bounded
  attempts, (b) authoritative terminal state per attempt, (c) honest
  announcements so dependents can compensate.

## Decision

1. **Atomic claims.** The claim query takes row locks
   (`with_for_update(skip_locked=True)`); on Postgres two workers can
   never claim the same row, on SQLite the database write lock serves the
   same guarantee. Claiming, expiry sweep, and reclaim-death all flush
   explicitly (sessions run `autoflush=False` — the sweep must be visible
   to the claim query inside the same transaction).
2. **Fencing on every terminal write.** `mark_running`, `mark_succeeded`,
   `mark_failed` accept `expected_owner`. If the lease has moved, the
   write is refused ("fenced"), the metric `work_fenced_writes_total`
   increments, and the zombie's result/failure is discarded. The
   reclaiming worker's attempt is authoritative.
3. **Lease heartbeats.** While a handler runs, a per-item task renews the
   lease every lease/3. A healthy slow worker is never reclaimed; a
   heartbeat that loses ownership stops immediately (its result would be
   fenced anyway). Graceful shutdown cancels heartbeats last.
4. **Loud deaths, same-transaction announcements.** `claim_due` returns a
   `ClaimBatch` — claimed, expired, and reclaim-dead items. The runner
   announces every death IN THE TRANSACTION THAT PERSISTED IT:
   - expired wait → `work.expired:<id>` signal (a lifecycle outcome, no
     dead-letter row, per ADR-0011 — but now announced);
   - reclaim-exhausted → dead-letter row + `work.dead:<id>` signal.
5. **Graceful shutdown is bounded draining.** `stop()` stops claiming and
   lets the in-flight batch finish within a grace period; past the grace
   period the task is cancelled and lease expiry + reclaim take over —
   no item is ever lost to a shutdown.
6. **Bounded intra-process concurrency** (`max_concurrency`, default 1)
   via semaphore-gathered batches — same guarantees, optional parallelism.
7. **Destructive work is schedulable.** The old "cannot schedule
   destructive capabilities" guard is replaced by the stronger generic
   guarantee of ADR-0014: the action runs only after an explicit human
   YES, enforced at wake time.

## Consequences

- Multiple WorkRunners (processes or replicas) are safe today, not "later":
  the same DB, the same schema, no coordinator.
- Honest semantics, stated once: execution is AT-LEAST-ONCE across
  attempts (crash + retry may repeat effects); successful completion is
  recorded EXACTLY ONCE per item (fencing), and every death is announced.
- New observability: `work_fenced_writes_total`, `work_reclaims_total`.

## Evidence

- `tests/integration/test_work_concurrency.py` (14 tests: exclusive
  claiming, zombie writes refused mid-run and post-completion, heartbeat
  protection, crash recovery, loud reclaim-exhaustion, expiry
  announcement, graceful shutdown, renewal ownership).
