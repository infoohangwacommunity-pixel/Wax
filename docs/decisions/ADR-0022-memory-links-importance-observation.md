# ADR-0022: Typed Memory Relationships, Importance, and Observation Time

Date: 2026-09-14
Status: Accepted
Extends ADR-0010 / ADR-0012 / ADR-0019.

## Context

Mission Phase 3 asks whether WAX needs typed relationships between
memories (supports / supersedes / derived_from / related_to), and §6.3
lists importance and observation-vs-creation time among the memory
metadata the runtime should evaluate. The audit at `c3c7b99` found:

1. **Relationships were implicit only.** Supersession existed as a
   column; consolidation recorded sources as `consolidated_from` inside
   the content JSON — unqueryable, untraversable. Retrieval could not
   follow any relationship: "which memories matter for this objective"
   (mission §49) was answered only by lexical overlap.
2. **No importance.** Ranking blended BM25 + recency + confidence only;
   the intelligence had no way to say "this memory matters more".
3. **No observation time.** Temporal reasoning ("what did I believe last
   month?" — mission §4.3) conflated *when a fact was written into WAX*
   with *when it was true/observed*; late-arriving evidence aged
   artificially.

## Decision

### Typed evidence edges, relationally modeled

`memory_links` table (migration `d6f8a2b4c9e1`): `from_memory_id`,
`to_memory_id`, `kind` ∈ {supports, contradicts, derived_from,
related_to}, `principal_id` (both endpoints must share the owner),
`created_by_execution_id` (provenance of the LINK decision), unique per
(from, to, kind). Both endpoints must be ACTIVE memories of the same
principal at edge-creation time; self-links are refused; re-linking is
idempotent (returns the existing edge).

**Supersession is deliberately NOT a link kind.** It is lifecycle
(`superseded_by` + status) — it changes what retrieval returns. Link
kinds are evidence relationships — they never rewrite lifecycle state.
The two mechanisms stay composable but non-overlapping.

### Retrieval traverses links — one hop, damped, bounded

`search_relevant` expands the top-3 hits with their ACTIVE one-hop
neighbors (≤5 per anchor), scoring each neighbor at 0.6× the current
top score. Neighbors never displace directly-relevant records (damped
scores sort below anchors), superseded/archived neighbors are excluded
(lifecycle governs expansion), and the expansion cannot fabricate
relevance for memories with no link path to a hit.

### Importance weights rank, never relevance

`memory_records.importance` (0.0–1.0, NULL = neutral 0.5) enters the
score as `+0.1 × (importance − 0.5) × bm25_norm` — it can lift or sink a
memory that is ALREADY lexically relevant, but multiplies the BM25
term so importance alone can never surface an off-topic memory.

### Observation time is first-class

`memory_records.observed_at` (NULL = observed at creation) replaces
`created_at` in the recency computation: evidence that entered WAX late
("yesterday I finished the exam", written today) ages from when it was
TRUE, not when it was stored.

### The intelligence proposes; the runtime disposes

- `memory.store` (v1.2.0) accepts `importance`, `observed_at`, and
  `links` (≤10 edges, kind-validated). Link targets are verified
  BEFORE the record is created — a refused link (foreign, inactive) is
  a LOUD error, never a silently skipped edge. The response reports the
  edges that landed.
- New `memory.link` capability (idempotent, both endpoints
  ownership-checked loudly, `existed` reported on relink).
- `memory.consolidate` now writes `derived_from` edges to every source
  (in addition to the JSON provenance), created BEFORE supersession —
  sources must still be ACTIVE for the edges to be valid. Derived
  knowledge stays traceable to its evidence through a queryable
  structure: "this conclusion came from these prior observations" is
  now a SELECT, not a JSON read (mission §6.8).

## Consequences

- "Which memories matter" now has a traversal answer, not just a
  lexical one — without embeddings and without a graph database (the
  abstraction is the typed edge; storage is a detail, mission §8).
- Multi-session temporal reasoning gets correct recency semantics.
- The `existed`/loud-refusal discipline closes two fake-mechanism
  patterns found during implementation (silent link skips,
  edges-after-supersession that could never exist).

## Non-goals

- Multi-hop traversal / PageRank-style link scoring: one hop covers the
  named failures; deeper traversal is evidence-free speculation until a
  retrieval failure demonstrates the need.
- Embeddings: ADR-0019's non-goal stands; links + BM25 solve relation
  traversal without new infrastructure.

## Proof

- `tests/integration/test_memory_links.py` — 14 tests: edge mechanics
  (idempotency, direction, self/foreign/forgotten denial, unlink,
  traversal), retrieval integration (neighbor joins with damped score,
  superseded neighbors excluded), importance lift/sink with
  no-fabrication guarantee, observed_at temporal ranking and
  no-fabrication, capability surface (store-with-links end to end,
  cross-principal refusal is loud, idempotent relink reports existed,
  consolidation writes derived_from edges to every source).
- Suite: 620 passing (606 before this batch). Live probe: PASS.
