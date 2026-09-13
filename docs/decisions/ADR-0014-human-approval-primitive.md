# ADR-0014: The Human-Approval Primitive — a Generic Authority Boundary

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX principal-engineer continuation session (fourth pass)

## Context

The agency gate could classify actions that need explicit human
authorization (EXTERNALLY_VISIBLE, FINANCIALLY_CONSEQUENTIAL, DESTRUCTIVE,
IRREVERSIBLE) — and could only DENY them honestly ("no human-approval
workflow exists yet"). The denial path was real, but the environment was
not actually capable of open-ended work that requires consent: every
consent-requiring objective hit a wall. The prior non-goal was recorded
as "a product decision requiring an approver identity story" — this ADR
is that story, told in runtime primitives, not product features.

## Research

- **OS permission elevation (sudo/UAC)** — a privileged action creates a
  bounded, expiring REQUEST; the principal decides; the decision
  authorizes a specific action, not a class of actions; every step is
  audited. Adopted wholesale.
- **OAuth authorization codes / CSRF tokens** — one-time use, bound to a
  request, replay-protected. Adopted as the consumption model: one
  approval authorizes exactly one attempt of one exact request.
- **Dual control / four-eyes principle (banking)** — the decider must be
  a DIFFERENT authority than the requester. In WAX this is structural:
  the requester is the AI (acting on a human's behalf); the decider is
  the human, authenticated through an interface credential — a path the
  AI has no access to.
- **Temporal `update_with_start` / human-in-the-loop workflow patterns**
  — signal-based resume of durable work; WAX's equivalent is that work
  fails honestly, the approval lands durably, and requeue/retry consumes
  it.

## Decision

**The primitive (no domain vocabulary anywhere):**

```
AI requests action
  ↓ agency gate decides: human authorization required (runtime, not model)
  ↓ fingerprint = SHA-256(principal, capability, canonical inputs)
  ↓ PENDING APPROVAL (idempotent by fingerprint; expires; audited)
    + approval.requested:<principal> on the event ledger
    + best-effort notification through the delivery router
  ↓ human decides via the HUMAN authority path
    (interface adapter → RuntimeBridge.submit_approval_decision;
     the deciding principal resolves from an interface credential)
  ↓ approved | denied | expired | cancelled   (all audited + announced)
  ↓ an APPROVED, unconsumed approval authorizes EXACTLY ONE retry of the
    EXACT SAME request — consumed on use (replay impossible)
```

**Security properties (each enforced by infrastructure):**

1. The AI can never CREATE approvals on demand — creation happens only
   inside the agency gate when the runtime decides consent is needed.
2. The AI can never DECIDE — there is no approve capability; decisions
   authenticate a human principal via the interface credential path (the
   same identity path as inbound messages). `approval.*` signal names are
   runtime-owned (wait-only for the AI).
3. Replay is impossible — consumption is one-time, bound to the consuming
   execution id.
4. Scope creep is impossible — mutated inputs produce a new fingerprint;
   the approval covers the request the human saw.
5. Staleness is impossible — pending approvals expire (default 24h,
   `WAX_APPROVAL_EXPIRY_SECONDS`) and the maintenance loop sweeps them to
   `expired`.
6. Ownership is enforced — only the approval's principal may decide or
   cancel it.

**Surfaces:**

- `pending_approvals` table (migration `f8d3b7a9c1e4`): fingerprint,
  safe scope summary (truncated values, no secrets), expiry, decision
  provenance (who/when/note), consumption columns.
- `ApprovalService` (wax.authority.approvals): create_or_get_pending,
  find_pending / find_approved_unconsumed, consume, decide, cancel,
  expire_due.
- Bridge: the agency gate routes through the approval gate (approved →
  consume → proceed; pending → honest `pending_approval` outcome carrying
  the approval id and expiry); `submit_approval_decision` is the human
  authority path; `match_approval_command` exposes the generic
  `/approve <id>` / `/deny <id>` grammar for any interface adapter.
- AI capability surface: `approval.list` and `approval.cancel` — the
  intelligence can inspect and withdraw its own requests and can compose
  (e.g. schedule a work item that waits on `approval.granted:<id>`), but
  can never grant.
- Durable work: a scheduled action that requires consent fails honestly
  at wake time with the approval id in the error; the owner's approval
  plus a retry/requeue completes it exactly once.

## Consequences

- "Approve payment / approve study plan / approve email" are all the SAME
  primitive with different fingerprints — no per-domain workflows.
- Notifications are best-effort; the approval exists durably regardless,
  and a failed notification is logged + audited (never faked success).
- The denial path is unchanged in strength: a denied approval keeps
  blocking, and the AI is told the honest state.

## Evidence

- `tests/integration/test_approvals.py` (16 tests: fingerprint stability
  and sensitivity, idempotent creation, ownership, decide-twice refusal,
  one-time consumption, expiry sweep, cancellation, full end-to-end
  approve/deny flows through the bridge, stranger rejection, command
  grammar, capability surface).
- Live probe step 3d: the full flow over real HTTP (pending → webhook
  approval → exactly-once execution).
