# ADR-0025: Retention and Forgetting — Decision Boundaries, Not Invented Policy

Date: 2026-09-14
Status: Accepted (boundaries documented; policy decisions remain founder-owned)
Context: OMEGA mission §19 (retention/forgetting), §20 (sensitivity), §59 (retention decision).

## Context

The OMEGA retention audit traced every table and every deletion site in
the runtime. Findings (verified at commit `10c2333`):

- Exactly ONE table is bounded today: `runtime_signals` (30-day
  retention + a 100k-row cap, leader-only, waiter-safe — ADR-0015).
- Eighteen of twenty tables grow without any lifecycle: verbatim
  conversation text (`processed_messages.request_text/response_text`),
  bridge episodic `memory_records.content`, `executions.checkpoint`,
  `conversations`, `delivery_records` (terminal rows retained for
  audit), `audit_events`, `pending_approvals` (all terminal states),
  `dead_letter_entries`, `artifacts` rows, `provisioned_resources`
  rows, `objective_executions`, `work_items` terminal rows,
  `memory_links`, and more. On disk, the content-addressed artifact
  cache (`.artifact-cache`) is additionally never pruned.
- Forgetting is REAL for access and SOFT for storage: `forget` /
  expiry flip `memory_records.status → 'forgotten'`; every retrieval,
  link, search, and context path filters them out (tested). The row,
  its content, and its summary remain in the database by declared
  design ("retained for audit"). There is NO hard-delete path anywhere
  for principal data; the only cascade vector (principal deletion) is
  itself unwired.
- `sensitivity` on memory rows is an honestly-marked RESERVED column
  (CV-8 fix, `f44a067`): no runtime path reads it. Wiring it without a
  privacy policy would be a false mechanism.
- The conversation lifecycle (active → idle → archived) is now wired
  into the maintenance pass (this cycle's fix); it marks state and
  deletes nothing.

The mission is explicit: "Do not invent a privacy policy", "Where
founder policy is required, create an ADR instead of inventing policy",
and "Do not create arbitrary deletion just to reduce database size."

## Decision

**1. No pruner is written in this cycle.** Every deletion boundary in
the table below is a founder policy decision. Writing a pruner before
the policy exists would either destroy evidence (violating
"LAW 3 — evidence beats claims" for past facts) or pretend a privacy
guarantee that was never decided.

**2. The runtime states its retention classes honestly.** The table
below is the authoritative classification; every future schema change
touching one of these classes must update it.

| Class | Tables / stores | Behavior today | Founder decision required |
|---|---|---|---|
| Operational state | `work_items`, `objectives`, `objective_executions`, `executions`, `execution_steps` | Terminal rows retained; lifecycle honest | Whether terminal execution history may eventually be archived/compacted |
| Memory (durable) | `memory_records`, `memory_links` | Soft delete only; rows retained | Whether "forget" must ever become erasure; default conversational memory TTL |
| Conversation / raw text | `conversations`, `processed_messages`, episodic bridge memories | Rows never deleted; verbatim text retained | THE central data-lifetime policy: may verbatim text be pruned, and what must survive (summaries, memory integrity, audit)? |
| Delivery records | `delivery_records` | Terminal rows retained "for audit and operator re-drive" | Whether delivered records may be pruned; whether operator re-drive tooling will exist |
| Audit / security records | `audit_events`, `dead_letter_entries`, terminal `pending_approvals` | Append-only in practice | Retention horizons, if compliance ever requires them |
| Artifacts | artifact rows + files + `.artifact-cache` | Files TTL-bounded via workspace reaper; rows and cache never pruned | Cache GC policy; artifact row retention |
| Identities | `principals`, `principal_credentials` | Soft-delete method exists, unwired | Account deletion semantics (erasure vs audit preservation) |

**3. "Forget" means access-revocable, today, and says so.** The
declared contract ("soft delete — record retained for audit, excluded
from retrieval") is now stated in `MemoryStatus` documentation. If the
founder decides a right-to-erasure semantics is required, that is a NEW
mechanism (hard delete + audit redaction), not a reinterpretation of
the existing one.

**4. Sensitivity stays RESERVED.** The column keeps its honest marker.
Its eventual semantics — whether it affects retrieval, context
inclusion, logging, export, retention, or deletion — is a privacy
policy decision (§20's questions are recorded, unanswered, in the
omega memory architecture document).

**5. Lifecycle marking ≠ retention.** The wired conversation lifecycle
and memory expiry change STATE, not row counts. This is deliberate:
continuity and audit need the rows; only policy can decide deletion.

## Consequences

- Honest growth: the runtime's database grows until the founder sets
  policy. This is now explicit rather than accidental.
- The founder gets a single decision sheet (the table above) instead of
  nine scattered questions.
- No mechanism in the runtime claims retention behavior it does not
  have; every false claim found in the audit was corrected in this
  cycle.
