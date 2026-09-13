# ADR-0012: Memory Lifecycle Completion + Context Assembly as a Mechanism

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX principal-engineer continuation session (third pass)

## Context

Two verified defects, both on the "environment serves evidence to
intelligence" path:

1. **The memory lifecycle was incomplete.** Supersession existed at the
   repository layer with no caller; `memory.store` could only create
   rows, so a correction ("the exam moved") left TWO active, contradicting
   records; there was no consolidation path from transient evidence to
   durable representation (the mission's §10 chain: evidence → evaluation
   → consolidation → durable representation → revision → supersession →
   forgetting was broken at every link after "evidence").

2. **Context assembly discarded what it collected.** ContinuityService
   fetched the active objective, conversation gap, summary, and last
   execution state — and the bridge delivered NONE of it. On a resumed
   conversation the model could not see the objective it was pursuing.
   Delivery was a fixed recipe (last-5-episodic style counts), with no
   budget discipline.

## Research

- **Systems consolidation (cognitive science)** — the hippocampus-to-
  cortex analogy: transient traces are re-evaluated and distilled into
  durable knowledge; the raw traces eventually retire. WAX's mapping:
  episodic rows → (model evaluation) → durable semantic record → sources
  superseded but audit-retained. The RUNTIME enforces mechanics; the
  MODEL owns meaning (no rule engine, per the mission).
- **Generative Agents (Park et al.)** — retrieval scored by recency ×
  importance × relevance, with *reflection* producing higher-level
  memories linked to their sources. WAX already had recency+relevance
  (ADR-0010); consolidation is the reflection analogue, with the link
  made machine-checkable (supersession chain / `consolidated_from`).
- **MemGPT/Letta** — memory paging and self-editing. The self-editing
  surface (store/revise/consolidate/forget through gated capabilities)
  is adopted; paging is unnecessary at WAX's scale.
- **Context assembly** — production LLM systems assemble context under a
  budget with priority tiers (system > task state > retrieved evidence).
  Tokenizer-free accounting (character budget, ≈4 chars/token) keeps the
  runtime provider-independent; model-advertised context limits can
  replace the configured constant without changing the contract (that
  negotiation is recorded as future work).

## Decision

**Memory lifecycle (capability surface):**

- `memory.store` gains `supersedes=<memory_id>`: an ownership-checked
  revision. The target is verified BEFORE the replacement is created (a
  bad request creates nothing); the old record stays for audit and
  leaves retrieval. Declared contradictions become supersession chains;
  UNdeclared contradictions surface as two ranked evidence lines —
  resolution belongs to the intelligence, not the runtime.
- New `memory.consolidate`: N active, principal-owned sources (≤ 20) →
  one durable record with `provenance="consolidation"`. Sources are
  superseded by default (audit-retained, retrieval-excluded) or kept
  active with the link recorded in `content.consolidated_from`. Every
  source is verified before anything is created — no partial state.

**Context assembly (runtime mechanism, not prompt recipe):**

- `wax.continuity.assembly`: evidence becomes labelled sections —
  objective (priority 0), conversation state (1), per-memory lines with
  their retrieval reason (2). Absent evidence produces no section; no
  filler, no fabrication.
- `assemble_evidence` fills a configured character budget by priority,
  prefers whole entries, and ANNOUNCES truncation (`[evidence truncated:
  context budget reached]`) — the model is never silently shown a
  partial picture. 500 memories against a small budget yield bounded
  context, never a database dump.
- The system prompt is stripped to the minimal persona + environment
  facts (the principal id, needed to compose runtime names like
  `interface.message:<principal>`). All context flows through evidence
  lines where it can be budgeted, ranked, and attributed.
- The principal id is delivered as an environment fact (like a PID),
  not as persona.

## Consequences

- The mission's memory chain is fully reachable through gated
  capabilities: store → revise → consolidate → expire (worker) → forget
  → (audit trail everywhere).
- The model now sees the objective it is resuming; conversations can
  survive interface and process boundaries in practice, not just in
  schema.
- Budget is character-based — honest and portable, but not token-
  exact; tokenizer-based accounting is a recorded upgrade once a
  provider contract exposes context limits.
- `memory.consolidate` is model-driven: nothing auto-consolidates.
  A periodic consolidation *prompt* (not rule) is a recorded future
  option.

## Evidence

- `tests/integration/test_memory_mechanisms.py::TestMemoryRevisionAndConsolidation`
- `tests/unit/test_context_assembly.py`
- `tests/integration/test_open_world.py::TestMemoryLifecycleThroughConversation`
- Probe: evidence lines observable in the live request path.
