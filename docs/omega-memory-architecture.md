# WAX Memory Architecture (OMEGA)

What memory in WAX actually is, as verified in code at the OMEGA cycle
(`b439aae`). Companion to ADR-0022 (typed links, importance,
observed_at), ADR-0025 (retention boundaries), and the deep audit in
`docs/constitutional-audit/memory-deep-audit.md`.

## The one-paragraph truth

WAX memory is a per-principal, typed, evidence-aware, lifecycle-aware
store with soft forgetting, BM25-plus-scoring relevance retrieval, and
typed evidence relationships. It is NOT a vector dump, NOT a prompt
injection pipeline, and NOT a privacy policy: forgetting is real for
access and soft for storage (rows retained for audit by declared
design), and hard retention is a founder decision recorded in
ADR-0025, not an engineering constant.

## Capture — two doors, both runtime-owned

1. **Bridge (automatic).** Every completed exchange writes ONE episodic
   record (`user_message` + `assistant_response`, truncated, provenance
   `user_statement`) — `bridge/service.py` step 9.
2. **The AI (gated).** `memory.store` requires the `memory.write`
   permission and passes the full gate chain. Optional `expires_at`,
   `importance`, typed `links`, and `supersedes` (verify-before-create,
   loud on cross-principal or wrong state).

No other writer exists. The model cannot write memory except through
the authorized capability.

## Lifecycle (each stage real, tested)

```
capture → normalize/score → store → retrieve → use
        → update (reinforce / weaken / contradict / supersede)
        → consolidate (provenance-preserving) → forget (soft) → [founder policy: erasure]
```

- **Supersession**: conditional UPDATE active→superseded with
  `superseded_by` set. Old evidence retained. The AI can reason "this
  was previously true, then changed" because the history is queryable.
- **Contradiction**: `memory.link(kind="contradicts")` — both sides
  coexist as evidence; retrieval expands one hop so conflicting
  evidence is VISIBLE, not silently resolved.
- **Consolidation**: `memory.consolidate` (≤20 sources) writes
  `derived_from` edges BEFORE superseding sources. Never creates a
  summary and destroys the evidence — mission §18 verbatim.
- **Forgetting**: `memory.forget` (destructive-flagged → agency gate)
  or the runtime's TTL reaper (leader-guarded, runs every 300s). Both
  flip status to `forgotten` + audit event. Already-forgotten is an
  honest no-op; superseded rows refuse loudly (real state conflict).
- **There is no `archived` state.** The dead enum member was removed in
  this cycle: a lifecycle state with no writer is a false mechanism.

## Retrieval — relevance is a mechanism, not a vibe

`memory.search` / context assembly use BM25 over content + recency +
confidence + importance, with bounded one-hop link expansion
(budget-aware, ownership-checked at every hop). `importance` (declared
by the creator) and retrieval relevance (computed) are deliberately
different axes — mission §16.

## What memory CANNOT do (honest)

- No hard deletion / erasure path exists (ADR-0025).
- No shared/organizational memory (per-principal only; boundary
  documented in the model file).
- No cross-principal reads: every retrieval path filters
  `principal_id` at the query level; link/consolidate/forget
  additionally verify ownership in the capability layer; `unlink`
  now refuses missing or cross-principal endpoint pairs.
- `sensitivity` is RESERVED — written, never read, honestly marked.
  Its semantics require the founder privacy policy (§20 questions
  recorded in ADR-0025).
- Bridge episodic rows carry no `expires_at` and no sensitivity —
  they are the retention-policy exposure documented in ADR-0025.

## Evaluation coverage

Recall, irrelevance, update/supersession, temporal reasoning,
contradiction coexistence, consolidation provenance, forgetting, and
retrieval-poisoning scenarios are covered by the memory evaluation
suite (ADR-0024's 10 dimensions) plus the unit suites — see
`docs/omega-evaluation-report.md` for exact coverage and gaps.
