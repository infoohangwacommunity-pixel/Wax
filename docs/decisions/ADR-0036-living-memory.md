# ADR-0036: Living Memory — Objective Linkage + Consolidation Provenance + Conflict Graph

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 3 — Living Memory (OMEGA Implementation Directive)

## Context

WAX memory already supports (per ADR-0010, ADR-0012, ADR-0019, ADR-0022):
- `kind` (episodic/semantic/procedural/contextual/external)
- `confidence` (0.0-1.0)
- `importance` (0.0-1.0)
- `observed_at` (vs `created_at`)
- `superseded_by` (backward chain)
- `MemoryLinkRecord` with kinds: supports, contradicts, derived_from, related_to
- `memory.store`, `memory.search`, `memory.forget`, `memory.consolidate`, `memory.link` capabilities

The directive (Phase 3) asks for richer "living memory":

- Confidence evolution (track changes over time)
- Contradiction preservation (already supported via contradicts link)
- Supersession (already done)
- Relationship graph (already done)
- **Dependency edges** (NEW — `depends_on` link kind)
- **Conflict graph** (NEW — `conflicts_with` link kind, more general than `contradicts`)
- **Consolidation provenance** (NEW — forward chain on consolidated records)
- **Retrieval weighting** (PARTIAL — importance exists; retrieval uses it for ranking)
- **Replay** (NEW — the consolidation chain is now queryable for audit)
- Ownership (already done)
- **Objective linkage** (NEW — `objective_id` field on MemoryRecord)

## Decision

### 1. Add `depends_on` and `conflicts_with` link kinds

- `depends_on`: A needs B to be true. If B is later contradicted, A's
  confidence should drop. (Future cycle: automated confidence propagation
  via dependency edges — out of scope here.)
- `conflicts_with`: A and B cannot both be true simultaneously. More
  general than `contradicts` (which means "B is evidence AGAINST A");
  `conflicts_with` is symmetric ("A and B are mutually exclusive").

Both are added to `MEMORY_LINK_KINDS` so they're accepted by
`memory.store` (links parameter) and `memory.link` capability.

### 2. Add `objective_id` field to MemoryRecord

Optional FK to `objectives.id`. When present, retrieval can ask
"what memories do I have that bear on THIS objective?" — the
directive's "what objective I support" answer.

The `memory.store` capability accepts an optional `objective_id`
parameter. The runtime validates ownership (the objective must belong
to the calling principal) before attaching it.

### 3. Add `consolidation_sources` field to MemoryRecord

JSON list of source memory IDs. When `memory.consolidate` creates a
new record from multiple sources, this field records the source IDs.
This is the FORWARD provenance chain (the new record's "where I came
from"); the existing `superseded_by` is the BACKWARD chain (the old
records' "what replaced me"). Both are queryable for audit.

### 4. Retrieval weighting

The existing `MemoryRepository.search_relevant` already orders by
relevance score. The `importance` field weights the rank (NULL =
neutral 0.5). This ADR does NOT change the retrieval algorithm — the
existing implementation matches the directive's "retrieval weighting"
requirement. A future ADR may add importance-aware re-ranking if the
current ordering proves insufficient.

## Alternatives considered

### Alternative 1: Add a separate `MemoryDependency` table

Rejected — `MemoryLinkRecord` already exists and supports arbitrary
typed edges. Adding a new table would duplicate the schema. The
`depends_on` and `conflicts_with` kinds fit naturally into the
existing edge model.

### Alternative 2: Store consolidation sources in the content JSON

Rejected — the existing `memory.consolidate` already puts
`consolidated_from` in the content when `supersede_sources=False`. But
when sources ARE superseded (the default), the content does NOT carry
the source IDs. A dedicated column makes the forward chain queryable
regardless of the supersession setting.

### Alternative 3: Add a confidence-evolution history table

Considered but deferred — tracking every confidence change as a
separate row would balloon the memory table. The current model
captures confidence as a snapshot at write time; supersession is the
mechanism for "confidence changed" (the old record is superseded by a
new one with updated confidence). A future ADR may add a confidence-
change audit log if richer evolution tracking is needed.

## Consequences

### Positive

- Memory is now explicitly linked to objectives, enabling objective-
  scoped retrieval ("what do I know that supports THIS objective?").
- The consolidation provenance forward chain makes audit queries
  like "what was consolidated into this memory?" a single SELECT,
  not a graph traversal.
- The `depends_on` and `conflicts_with` link kinds formalize the
  dependency/conflict structure the directive requires.

### Negative

- One new migration (objective_id + consolidation_sources columns).
- The `memory.store` capability now has one more optional parameter;
  the API surface grows.

### Neutral

- The new link kinds are validated exactly like the existing ones
  (ownership-checked, unique per (from, to, kind) edge).

## Security

- `objective_id` is ownership-checked: a principal cannot attach
  their memory to ANOTHER principal's objective (which would leak
  that the objective exists).
- The new link kinds do not bypass any existing boundary — they
  are still subject to `MemoryLinkRecord`'s principal-scoped
  UniqueConstraint and the calling principal's ownership of both
  endpoints.

## Tests

`tests/integration/test_living_memory.py` covers:

- `depends_on` link creation
- `conflicts_with` link creation
- `objective_id` linkage in `memory.store` (success + ownership
  rejection for wrong principal's objective)
- `consolidation_sources` field is populated by `memory.consolidate`
- Backward chain (`superseded_by`) and forward chain
  (`consolidation_sources`) are both queryable after consolidation
