# ADR-0011: Durable Waiting — Wake Conditions, Runtime Signals, Work Requeue

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX principal-engineer continuation session (third pass)

## Context

Durable work existed (ADR-0004) but its waiting was a **timer**: a work
item could only wait for a clock time (`wake_at` / `available_at <= now`).
The mission distinguishes the two explicitly:

> The underlying requirement is not timers. It is: WAIT → PERSIST →
> SUSPEND → WAKE → RESTORE → CONTINUE, where the condition may be a time,
> another event, a dependency becoming available, an external service
> response, a resource becoming available, a human response, another
> execution completing.

Five of those seven condition shapes were unrepresentable. The
intelligence could say "in one hour" but not "when the user replies",
"when that work finishes", or "when the payment webhook fires".

## Research

Prior art examined (conceptually, against WAX's constraints — single
Postgres/SQLite, in-process runner, lease-based claiming):

- **Temporal / Cadence** — durable timers + *signals*: external events
  delivered to workflows via a durable queue. Right semantics, wrong
  cost: a delivery-per-waiter machinery WAX does not need.
- **Erlang/OTP** — selective receive with `after` timeouts: a process
  waits on a pattern in its mailbox. The pattern-matching analogue
  (name correlation) was kept; the per-process mailbox was replaced by
  a shared ledger because WAX "processes" are DB rows, not VM
  processes.
- **POSIX condition variables / futexes** — wait on a condition, wake on
  `notify`. Key insight adopted: **signals are broadcast** — every
  waiter wakes, each consumes the event independently.
- **Postgres SKIP LOCKED queue patterns / SQS** — claim semantics,
  visibility timeouts, at-least-once delivery. Already WAX's lease
  design; extended rather than replaced.
- **Kafka consumer offsets** — the *watermark* idea: a waiter's position
  determines which events are "new". Adopted to make waiting never
  retroactive.

## Decision

1. **Signals are persisted facts, not messages.** `runtime_signals` is an
   append-only ledger: `(name, payload, emitted_at, emitted_by)`.
   Emission is one INSERT — no fan-out, no delivery failures, crash-safe
   by construction. Waiting is implemented by the runner's claim query
   correlating waiters against the ledger; event waiting therefore has
   *identical* lease/retry/recovery semantics to time waiting.

2. **Wake conditions are a closed two-kind set** on `work_items`:
   `wake_kind = "time" | "event"`, plus `wake_event` (the signal name)
   and `wake_watermark` (only signals emitted strictly AFTER the
   watermark can wake the item — no retroactive wakes). `expires_at`
   bounds the wait: an unmet condition at the deadline dies honestly
   ("condition not met") rather than waiting forever. Expiry is a
   lifecycle outcome, not an execution failure — no dead-letter row.

3. **Claim semantics** (`claim_due`): time items claim when due; event
   items claim when their floor passed AND a matching signal exists
   after their watermark. Broadcast: every waiter on a name wakes.
   Retries re-consume the same signal (at-least-once, consistent with
   lease-reclaim semantics). Deadline sweep precedes claiming.

4. **Namespaces encode trust.** `interface.*` (emitted by the bridge
   when a principal's message is ACCEPTED) and `work.*` (emitted by the
   runner on terminal work states: `work.succeeded:<id>`,
   `work.dead:<id>`) are runtime-owned. The intelligence may WAIT on
   these facts but can never EMIT them — `signal.emit` refuses reserved
   prefixes, so the model cannot forge "the user replied" or "the work
   finished". Everything else is open for gated emission.

5. **Emission points are runtime boundaries.** The bridge announces
   `interface.message:<principal>` at ACCEPTANCE (after security gates,
   before intelligence) — deliberately early, so work scheduled while
   processing message N waits for message N+1. Deduplicated re-delivered
   messages are not re-announced (a duplicate webhook is not a new human
   interaction). Security-gate rejections never announce (a refused
   prober is not an interaction). The runner announces terminal work
   states in the same transaction as the status change.

6. **The intelligence composes; it never tells the runtime how.**
   `work.schedule` accepts `wake_event` (mutually exclusive with
   `wake_at`/`delay_seconds`), optional `not_before` floor, optional
   deadline. Scheduled work carries `execution_id` (the caller's trace)
   so work → execution → objective traceability holds.

7. **`work.requeue`** closes the recovery loop: dead work (retries
   exhausted, e.g. by a provider outage) can be requeued by its owner as
   a fresh, provenance-linked item (`payload.requeued_from`). Dead items
   stay for audit; succeeded items cannot be double-run; live items
   still retry on their own.

## Consequences

- A reminder remains a composition (time wake → message.send) — unchanged.
- "Continue when the user replies", "run B when A finishes", "react to an
  external integration event" become compositions of the same primitive.
- No new infrastructure: same DB, same runner, same poll loop.
- Signal rows accumulate; the ledger is small (one row per accepted
  message + terminal work states) and can be pruned by a retention pass
  later (recorded non-goal).
- **Uncertainty:** the runner polls (2s default) rather than being
  notified — deliberate; polling keeps the mechanism crash-safe with zero
  new infrastructure. LISTEN/NOTIFY or equivalent can shorten latency
  later without semantic change.

## Future (recorded, not built)

Dependency/package acquisition (mission §18): the acquisition loop exists
as `scratch.workspace` (provision) + `code.run` (execute, scrubbed env,
egress governed by the network boundary, ADR-0008). A package-acquisition
mechanism would need: allowed-source allowlists, integrity verification,
per-principal caches, reproducibility, and supply-chain policy — justified
only when a real workload needs dependencies the runtime image and
code.run cannot reach. Building it now would be speculation.
