# WAX Agent Runtime (OMEGA)

How reasoning becomes execution, verified at `b439aae`. Companion to
ADR-0013 (durable work), ADR-0020 (objective lifecycle),
`docs/constitutional-audit/execution-audit.md`.

## The generic loop (mission §28) as it actually executes

1. **Observe** — an interface adapter delivers a RuntimeRequest; the
   bridge resolves/creates the principal from the interface credential
   (identity is runtime-resolved, never model-claimed).
2. **Security gate** — rate limit → cost cap → abuse/injection verdicts;
   untrusted content is wrapped as data, never as instructions.
3. **Durable acceptance** — objective created, execution started,
   idempotency record committed BEFORE intelligence runs. A crash here
   leaves honest state the recovery scan can reconcile.
4. **Assemble context** — the seven priority sections (see the context
   architecture document), budget negotiated from the provider limit.
5. **Reason** — provider-agnostic LLM call over the failover chain
   (retry + breaker per candidate).
6. **Request** — the model may request capabilities as tool calls;
   every request passes the ONE shared gate (agency → approval →
   budget → authority → invoker). Results return as tool messages.
7. **Observe results / update memory** — exchange recorded as episodic
   memory; execution steps record every attempt's evidence.
8. **Complete honestly** — `succeeded` lands only when the DB holds no
   outstanding durable work for the objective (a fabricated terminal
   state is impossible; `67357e0`).
9. **Continue / wait / ask human** — work can be scheduled with TIME or
   EVENT wake; a pending human approval IS the objective awaiting the
   human (evidence sync, ADR-0020).

## Durable execution and time (mission §41)

`work.schedule` is the only time mechanism: TIME wake (`wake_at`/
`delay_seconds`, capped at 30 days) or EVENT wake (`wake_event`,
watermark-correlated against the persistent signal ledger so work
cannot miss signals emitted before its scheduling instant). At wake,
the SAME gate chain runs again in the work handler. Retries with
backoff are bounded by `max_attempts`; death is loud (`dead` status +
dead-letter); `work.requeue` is the recovery path (provenance-linked;
the dead row is retained). Honesty-expiry: waiting work past its
deadline is marked dead, never silently dropped.

A reminder, a recurring report, a delayed workflow — all are
compositions of this primitive. The runtime does not know what they
are "for" (mission §41 verbatim).

## Loop safety (mission §31)

Iteration budgets (`max_tool_rounds`), per-execution resource budgets
(LLM calls, tokens, capability invocations, CPU-seconds) enforced
deny-by-default, wall-clock timeouts at every external boundary,
attempts caps with exponential backoff, dead-letter after exhaustion,
approval gates for destructive/irreversible/externally-visible actions,
human escalation via the approval primitive, durable checkpoints before
any intelligence runs, and orphan recovery (lease expiry + fencing for
work; recovery scan for bridge executions). The runtime can say "this
is no longer making legitimate progress" without knowing the domain.

## Multi-worker composition (mission §30)

The runtime provides the generic pieces — creating work, isolating
work, assigning authority (principal ownership), artifact exchange,
cancellation, joining results, recovery, resource budgeting — and does
NOT hardcode a worker count or a role catalogue. The intelligence
decides whether decomposition is worthwhile; today's mechanism is
scheduled durable work (one or many items per objective), with the
coordination primitives (signals, wait conditions, artifacts) already
generic.

## Provenance of every effect

`executions` + `execution_steps` record every attempt (inputs, outputs,
status, capability, error); `audit_events` record every authorization
decision; the signal ledger records every runtime fact; delivery
records track every outbound message. The chain objective → context →
decision → request → authorization → execution → evidence → delivery is
traceable from the database alone.
