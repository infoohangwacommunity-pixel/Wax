# ADR-0021: Durable Outbound Delivery Lifecycle

Date: 2026-09-14
Status: Accepted

## Context

Mission §55: "Do not silently swallow outbound delivery failures. If WAX
successfully completes work but cannot deliver the result: That should
become recoverable state... Keep: execution result, separate from:
delivery result."

The audit at `48b56cd` found the runtime held delivery as either
fire-and-forget or a graveyard:

1. **Bridge reply path** — the WhatsApp adapter sent the reply inline;
   on failure the app's `on_send_failure` hook wrote a dead-letter row.
   Nothing ever re-drove dead-letter rows: the user's completed work
   existed, the reply was lost, and no mechanism could ever recover it.
2. **`message.send` capability** — synchronous and honest (failure
   raises; scheduled work retries at the work layer). Defensible;
   unchanged here.

## Decision

### Delivery is durable runtime state with an honest lifecycle

New `delivery_records` table (migration `c4d6e8f0a2b3`) and
`wax.runtime.delivery_queue.DeliveryQueue`:

```
pending (attempt 0, due now)
  → attempt via the DeliveryRouter
      → delivered   (interface sender returned without raising)
      → pending/retrying (attempts+1, last_error preserved,
                          next_attempt_at = now + exponential backoff)
          → maintenance sweeps retry due records
              → delivered
              → failed (terminal: attempts exhausted, or the
                        deliverability horizon passed)
```

- **Interface-agnostic.** The queue sends through the runtime's
  DeliveryRouter only; whatever interface adapters are attached is
  whatever gets served. No WhatsApp code in the queue.
- **Honest success.** `delivered` requires the sender call to return
  without raising. A missing sender is a *retryable* failure, never a
  success and never a silent drop.
- **Deliverability horizon** (`delivery_max_age_seconds`, default 24h —
  interface-agnostic; it matches WhatsApp's template-less
  customer-service window): pending records older than the horizon are
  terminal-failed with an explicit reason. The runtime does not retry
  forever what can no longer be delivered.
- **Provenance.** Every record carries principal, source
  (`bridge_reply | capability | approval_notify | work`), execution id
  when known, the verbatim text, attempt count, and last error.
- **Observability.** `delivery_records_{delivered,retrying,failed}_total`
  counters; structured `delivery.enqueued / delivered / retry_scheduled
  / failed_terminal / expired` logs. The record itself is queryable
  state.

### The bridge reply path uses the queue

The adapter's send-failure hook now resolves the principal from the
recipient's verified credential (`whatsapp_phone`) and enqueues a
delivery record with the first attempt already spent (honest backoff
start), replacing the dead-letter row. `whatsapp.send` dead-letter rows
are gone: recovery, not audit, is the mechanism. The dead-letter table
remains for what it is for (processing failures, work deaths).

### Maintenance owns the retries

`run_maintenance_pass` gains a delivery-retry sweep (leader-elected,
same pass as approval expiry and ledger retention). The sweep requires
the LIVE services container — retrying against an empty router would
burn attempts on "no sender attached" — so `run_maintenance_pass`
accepts `services=None` (legacy callers skip the sweep honestly) and
the lifespan passes `app.state.services`.

## Consequences

- A completed reply can no longer be lost: worst case is a terminal
  `failed` record with the text and error preserved — visible,
  queryable, operator-actionable.
- Delivery result remains separate from execution result: the bridge
  execution completes regardless; the record tracks the delivery truth.
- Backoff defaults (60s, ×2, 5 attempts) and the horizon are settings,
  not hardcoded policy.

## Non-goals

- Delivery receipts/read-receipts (provider-side status webhooks feed
  the interface layer, not the runtime queue).
- `message.send` becoming enqueue-first: its synchronous-honest
  semantics (raise on failure; scheduled work retries at the work
  layer) are correct for an intelligence-invoked action. The queue is
  for the runtime's OWN outbound obligations.

## Proof

- `tests/integration/test_delivery_lifecycle.py` — 5 tests over the
  real ASGI app and webhook path: failed reply → pending record with
  principal/text/error/backoff; maintenance retry delivers when the
  interface recovers (and does not burn attempts during backoff);
  exhaustion is terminal; horizon expiry; missing sender is retryable.
- Suite: 606 passing (601 before this batch). Live probe: PASS.
