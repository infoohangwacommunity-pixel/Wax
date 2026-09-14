# ADR-0028: Capability Invocation Idempotency — Claim Ledger, Lease, Honest Takeover

Date: 2026-09-14
Status: Accepted
Context: POST-OMEGA mission §29 (capability idempotency — "test the failure matrix"); CV-19.

## Context

`CapabilityInvocationRequest.idempotency_key` was declared in the contract
("the runtime validates the idempotency key has not been used") while
nothing read it — a false mechanism at the SOLE effect point. The
constitutional audit's open-items list recorded it honestly (ADR-0026
ranked primitive list), and the POST-OMEGA mission §29 demanded the
semantics be investigated deeply and implemented generically — "not
simply an idempotency column and declare victory."

The invocation path has real failure modes the contract must survive:

- **identical request replay** — the intelligence (or a network retry,
  or a duplicate webhook delivery) re-sends the same request shape;
- **retry after failure** — the first attempt failed; is re-execution
  honest?
- **crash after effect but before acknowledgement** — the runtime died
  between claiming a request and recording its outcome; the effect state
  is UNKNOWN;
- **concurrent duplicates** — two bridge calls, a bridge call and a work
  handler, or two replicas, race the same request shape;
- **scheduled execution** — durable work may replay a capability call
  after a crash (same at-least-once semantics as the work queue itself).

## Decision

**1. A database-owned claim ledger.** Table `capability_invocations`
(migration `a8c2e6f0b4d6`), one row per
`(principal_id, capability_name, idempotency_key)` — enforced by a FULL
unique index, so at-most-one-claim is a DATABASE property, not a
convention. The claim is inserted atomically
(`INSERT .. ON CONFLICT DO NOTHING`) BEFORE the implementation runs,
through its own database session so the claim survives any rollback of
the caller's surrounding transaction.

**2. The key is caller-chosen request metadata.** The intelligence
decides whether a request shape needs at-most-once semantics by
including the declared transport field `idempotency_key` in tool-call
arguments. `lift_idempotency_key` removes it from the inputs BEFORE the
authority gate: approval fingerprints stay about the operation, not the
retry handle. Capabilities do not opt in or out; the ledger knows
nothing about what a capability is for. This is a mechanism, not a
policy.

**3. Verdicts.**

- `claimed` — proceed; you own the claim.
- `replay` — a `succeeded` row exists: return the RECORDED outcome
  (`idempotent_replay=True`); the effect did not run again.
- `executing` — a live claim exists: refuse with `outcome="duplicate"`;
  retrying with the same key obtains the recorded outcome.
- `takeover_lost` — another retry won the takeover: refuse likewise.

**4. Failure and crash semantics are honest at-least-once.** A `failed`
attempt records no outcome, so an identical retry takes the claim over
and re-executes — honest, because the first attempt produced nothing to
replay. A claim abandoned mid-execution (process died between claim and
completion) holds status `executing` until its claim lease
(`capability_idempotency_claim_seconds`, default 900 s) expires; an
expired lease means the effect state is UNKNOWN, so an identical request
may take the claim over and re-execute — the SAME honest semantics the
durable-work queue documents. Fencing is inherent: the takeover is a
conditional UPDATE, so only one retry wins. The runtime does not pretend
at-most-once for non-transactional side effects; the ledger bounds
duplicate EXECUTION, it cannot make partial effects atomic.

**5. The ledger never deletes rows.** Replay evidence is audit evidence.
Rows are bounded by their unique key — one row per request shape, ever.

**6. Authorization precedes claiming.** A denied, unknown, unavailable,
or invalid request never consumes the caller's key (the claim happens
after authorization and input validation, before execution).

## Consequences

- Duplicate effects from replayed requests are eliminated for any
  capability when the caller supplies a key — across live bridge calls,
  work handlers, and (with the lease) process crashes.
- The intelligence gains a universal primitive ("make this request
  shape at-most-once") without any per-capability special-casing.
- The failure matrix is tested
  (`tests/integration/test_capability_idempotency.py`): replay without
  re-execution, concurrent exactly-once, live-claim refusal, failed-
  attempt retry, expired-lease takeover, principal isolation, no-key
  pass-through, append-only evidence, denied-request key preservation.
- Cross-dialect honesty: SQLite returns naive datetimes for
  `DateTime(timezone=True)`; lease comparisons normalize at the
  boundary (`_aware`) and the takeover runs with
  `synchronize_session=False` so the fence is a pure database-side
  conditional UPDATE.
- The register's open item "idempotency_key honoring" is CLOSED.
