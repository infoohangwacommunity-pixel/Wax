# ADR-0017: Maintenance-loop leader election and multi-instance correctness

Date: 2026-09-13
Status: Accepted
Supersedes the "multi-node leader election" non-goal in reconciliation-4.

## Context

WAX is designed to run as multiple instances (gunicorn workers,
replicas). Two periodic subsystems exist:

1. **Durable-work claiming** — already multi-instance safe by
   construction: atomic claims (`FOR UPDATE SKIP LOCKED`), lease
   fencing on terminal writes, heartbeats (ADR-0013).
2. **The maintenance loop** (approval expiry, signal-ledger retention,
   memory expiry) — IDEMPOTENT, so concurrent passes converge to the
   same end state; but N instances running identical sweeps wastes
   work, multiplies row contention, and made "who swept?" ambiguous in
   logs.

Re-evaluated against the Universal Primitive Test: leader election for
periodic lifecycle work is infrastructure (it exists for ANY workload).
At multi-instance scale it stops being a nice-to-have: redundant sweeps
of a bounded ledger across 20 replicas are real contention.

## Decision

Per-pass leader election over the DATABASE (no new infrastructure):

- **Postgres**: `pg_try_advisory_lock(0x5741_5852)` on a dedicated
  session. The winner runs the pass with the session (and lock) held;
  the lock is released by closing the session — crash-safe by
  construction (a dead instance's lock dies with its connection).
- **SQLite**: single-writer database — concurrent instances are
  impossible by construction. Every instance is the leader. Documented
  semantics, not a missing feature.
- **Unknown dialects**: fail OPEN (act as leader). Sweeps are
  idempotent; skipping them on an unknown dialect would be worse than
  double-running them.

The decision is VISIBLE:
- the pass result carries `leadership_mode`;
- followers log `runtime.maintenance.follower` and meter
  `maintenance_leadership_total{role="follower"}` — a silent skip is
  forbidden (a follower that pretends nothing happened looks identical
  to a broken sweeper);
- leaders meter `role="leader"`.

Per-pass election (try-lock each pass) rather than long-held leadership:
a leader that stalls simply loses the next pass; there is no lease to
renew, no TTL to tune, and no split-brain window beyond one pass.

### What was NOT needed

- Work-claim election: claims are already safe to run concurrently;
  electing a single work runner would REDUCE throughput and create a
  failover dependency for zero correctness gain.
- Distributed cron: out of scope — the loop is a lifespan task per
  instance; correctness (not efficiency) is guaranteed by the election.

## Consequences

- Multi-instance deployments run each maintenance pass on exactly one
  instance (per pass), with the election visible in logs, metrics, and
  return values.
- The follower path is tested end-to-end (sweep genuinely does not
  run; approval stays pending; metric increments).
- SQLite deployments behave exactly as before (single_writer mode).
