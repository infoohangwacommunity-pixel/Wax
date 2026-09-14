# WAX Context Architecture (OMEGA)

What actually reaches the model, verified at `b439aae`. Companion to
ADR-0023 (context sections + artifacts) and
`docs/constitutional-audit/context-audit.md`.

## Definition (LAW 6, implemented)

Context is the model's current operational view of reality — NOT
conversation history. Conversation is one section among seven.

## Assembly — what the runtime builds

`ContinuityService.build_context()` assembles a `ContinuityContext`;
the bridge converts it into labelled SYSTEM evidence lines via
`build_evidence_sections()` + `assemble_evidence()`:

| Priority | Section | Content |
|---|---|---|
| 0 | OBJECTIVE | The active objective's description + status + execution evidence. Reachable on the LIVE path: the bridge writes `conversation.attach_objective` the moment both ends exist (CV-6 fix) |
| 1 | ACTIVE_WORK | Durable work pending/running/waiting for this principal |
| 2 | CONVERSATION | Recent exchange summary — one component, not the whole |
| 3 | MEMORY | Ranked memories (BM25 + recency + confidence + importance, bounded one-hop link expansion) |
| 4 | ARTIFACTS | Principal-owned artifact metadata (names + IDs, never host paths) |
| 5 | ENVIRONMENT | Available capabilities, current time, principal id |

## Budgeting — importance ≠ inclusion

The evidence budget is NEGOTIATED from the provider's advertised
context limit (ADR-0015 `derive_context_budget`) with a configured
fallback; `assemble_evidence` fills sections in priority order and
truncates lower-priority material under pressure. The objective section
cannot be silently discarded by a large conversation — it is priority 0
and the conversation is priority 2.

## Provenance

Every evidence line is labelled with its section origin; memories carry
id/kind/score so "why did this enter context?" is answerable from the
execution trace (steps + audit events). Internal per-item provenance
(score components, selection reason) is a recorded upgrade path, not a
false claim.

## Across sessions

A conversation boundary is not a cognitive boundary: after hours or
weeks, context reconstructs from durable state — objectives, waiting
work, memories, artifacts — not from pretending the world began again
(mission §25). The conversation row itself is lifecycle-marked
(idle → archived, wired this cycle); continuity lives in memory and
objectives, not in resuming an old chat row.

## Provider portability

Evidence lines are plain text with no provider-specific formatting;
tool specs are built from capability descriptors; token accounting and
context limits are adapter-owned (ADR-0024). Swapping providers does
not touch the assembly mechanism.

## What context CANNOT do (honest)

- No per-item selection-reason provenance yet (score composition only).
- No cross-interface environment state beyond the attached adapters.
- The conversation section is summary/recency-based, not a full replay
  — by design (LAW 5: memory is not a dump).
