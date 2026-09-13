# ADR-0019: Memory retrieval upgrade — two-stage recall + BM25 ranking

Date: 2026-09-14
Status: Accepted
Amends ADR-0010 (retrieval scorer) and ADR-0012; supersedes the
"tsvector/embedding memory retrieval" non-goal in reconciliation-4.

## Context

Memory retrieval searched only the NEWEST 200 active memories with a
raw term-overlap scorer. Two structural weaknesses:

1. **Recall hole.** A principal whose memory count exceeds the pool
   permanently loses old-but-relevant memories — retrieval answered
   "what did you say lately?", not "what do you know?".
2. **Weak ranking.** Raw overlap treats a term shared by every
   candidate the same as a rare, identifying term. "project" in a pool
   of project notes contributes as much signal as a distinctive name.

Re-evaluated against the Universal Primitive Test: retrieval is
infrastructure (any interface, any model, any workload). The upgrade
must stay portable (tests and local dev run SQLite) and inspectable.

## Decision

Two-stage retrieval in `MemoryRepository.search_relevant` (contract
unchanged: `[(record, score)]` descending):

### Stage 1 — RECALL

- **Portable arm (default):** newest-N pool ∪ lexical matches — the
  top-8 longest query terms are matched against summary and serialized
  content (SQLAlchemy `ilike` over a cast; terms are alnum-only by
  construction, so no escaping hazard). An old on-topic memory is now
  reachable on EVERY dialect.
- **Postgres indexed arm (production):** migration `d9e4f2a8b1c7` adds
  a `GENERATED ALWAYS` tsvector column (`summary + content::text`) with
  a GIN index, maintained by the DATABASE, never written by the app.
  The ORM metadata deliberately does not declare it — it is derived
  index state, and SQLite's `create_all` must not render a tsvector.
  On SQLite the migration is an honest dialect-guarded no-op; the real
  chain (fresh/upgrade/rollback) is exercised by the alembic
  subprocess discipline tests.

### Stage 2 — RANK (portable, in Python, on purpose)

BM25 (Okapi, k1=1.2, b=0.75) over summary + serialized content:

- idf = ln((N − df + 0.5)/(df + 0.5) + 1) — rare terms dominate; a term
  every candidate shares contributes almost nothing
- tf saturation + document-length normalization — focused memories beat
  ramblers that mention a term once
- normalized by the pool max, then blended with the existing recency
  decay and confidence boost (the ADR-0010 blend survives)

Ranking stays in Python for inspectability and dialect portability; the
indexed recall keeps it cheap at production scale.

### Deliberate non-goal (re-evaluated, still legitimate)

Embeddings/vector retrieval: requires an embedding provider, a vector
store, and drift management between embedding generations. Lexical
recall + BM25 resolves the named failures at zero new infrastructure.
Revisit when cross-lingual or paraphrase retrieval is a real workload.

## Consequences

- Retrieval quality properties are pinned by tests: rare-term
  dominance, tf/length effects, recall beyond the recency pool,
  principal isolation, superseded exclusion, honest emptiness.
- The `alembic check` zero-drift guarantee is unchanged on SQLite. On
  Postgres, `search_vector` is expected DB-side derived state — a
  drift report naming it is not a regression (documented here).
- ADR-0010's portable scorer remains the re-rank stage; its "documented
  upgrade path" is now reality rather than a promise.
