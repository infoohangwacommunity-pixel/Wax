# ADR-0023: Context Sections for Active Work, Artifacts, and Environment; First-Class Artifact Records

Date: 2026-09-14
Status: Accepted
Extends ADR-0012 (memory and context) / ADR-0019 (retrieval).

## Context

Mission Phase 5 (§10–14) requires the context the intelligence receives
to cover the semantic sections of its current situation; §111 requires
that under a degraded budget the objective and critical state survive.
The audit at `3310ebb` found the context engine delivered only three
sections (objective, conversation, memory). Missing:

- **ACTIVE_WORK** — what durable work is outstanding and when it wakes
  (§99: a resumed objective reconstructs its pending actions; §115:
  "What remains? Why did WAX stop?").
- **ARTIFACTS** — §56 requires artifacts to be first-class (owner,
  integrity, lifecycle), explicitly NOT arbitrary filesystem paths;
  §100 requires artifact state distinct from memory.
- **ENVIRONMENT** — the interface the interaction arrived on.

Artifact state did not exist at all: `workspace.acquire` computed a
sha256 and left a file in a scratch workspace — the only trace was an
audit log line and whatever the model put in memory.

## Decision

### 1. First-class artifact records at the acquisition boundary

`artifacts` table (migration `e1a3c5e7b9d2`): owner (principal FK),
workspace link, workspace-RELATIVE path (never an absolute host path —
the host layout is an implementation detail, not evidence), sha256,
size, source (`workspace.acquire`), execution provenance, metadata, and
an `expires_at` mirroring the workspace TTL (the file cannot outlive
its workspace). `workspace.acquire` now writes the record where it
already verifies integrity and reports `artifact_id` in its result.
Artifact state is a separate table — not memory, not audit, not the
filesystem (§100).

### 2. New evidence sections with the mission's priority shape

`ContinuityContext` gains `active_work`, `recent_artifacts`, and
`environment`; the assembler emits labelled, budgeted sections:

```
objective (0) > active_work (1) > conversation (2) > memory (3)
              > artifacts (4) > environment (5)
```

- ACTIVE_WORK carries metadata only — capability name, status, wake
  kind/time, attempts — never payload contents (payloads may carry
  arbitrary inputs; the execution trace is the place for those).
- ARTIFACTS carries filename, integrity PREFIX, size — never bytes
  (the media invariant: the model sees text, never bytes).
- Both new conversations and resumed conversations see outstanding work
  and artifacts: "keep working on this while I am away" must survive a
  conversation boundary (mission §17).
- Under budget pressure the ordering holds: the objective and the
  active-work line survive; artifacts and environment are dropped and
  every cut is announced (existing truncation contract).

The conversation-vs-memory relative order is unchanged from the prior
design (its rationale is documented there); the mission's §12 ordering
is treated as advisory input ("research and implement the actual
ranking"), and what this ranking implements is: losing the objective
loses the thread; losing the work view loses resumption; losing one
memory or the artifact list costs less.

## Consequences

- A resumed objective now sees: objective, what is still pending and
  when it wakes, which artifacts exist with their integrity, and the
  interface in play — the §99 reconstruction set, minus checkpoint and
  approvals (approvals already surface via `awaiting_human` evidence
  and the approval capabilities).
- Acquisition produces queryable artifact state; integrity is part of
  the record, not a recompute-on-demand hope.
- The assembler's budget contract is unchanged — it fills whatever
  budget the negotiation provides (ADR-0015/0018 still govern).

## Non-goals

- Artifact records for `code.run` outputs: runs do not enumerate
  produced files yet; recording a fake enumeration would be dishonest.
  When run outputs become enumerable, the `source` vocabulary extends.
- Delivering file BYTES into context: files stay out of the prompt; the
  runtime can later compose on-demand excerpting as a capability.

## Proof

- `tests/integration/test_context_sections.py` — 5 tests: acquisition
  creates a first-class record (owner/integrity/TTL/relative path, no
  host-path leakage), artifacts are principal-scoped, build_context
  carries active_work/artifacts/environment through the real
  capability chain, sections render in mission priority order, and a
  degraded 250-char budget keeps objective + active_work while
  dropping artifacts and announcing the cut.
- Suite: 625 passing (620 before this batch). Live probe: PASS.
