# Context Assembly Audit — Constitutional Audit

Scope: `src/wax/continuity/` (assembly, contracts, service), budget negotiation (`intelligence/context_limits.py`), consumption sites.

## Actual assembly mechanics (verified)

| Property | Reality | Class |
|---|---|---|
| Priority ordering | `objective(0) > active_work(1) > conversation(2) > memory(3) > artifacts(4) > environment(5)` — module constants (`assembly.py:41-46`); the code comment citing mission §12 contradicts its own ordering; ADR-0023 admits §12 is advisory | LIVE, hardcoded (mechanism constants — acceptable, comment fixed) |
| Budget negotiation | Provider-adapter-driven: `estimate_tokens`/`context_limit_tokens` duck-typed; tokenizer-exact when available, calibrated chars/token (clamp [2.0,6.0]) with provenance recorded; `MIN_BUDGET_CHARS=1200` floor | LIVE |
| Provider negotiation | Zero provider structure in core assembly — adapter surface only | LIVE, clean |
| Degradation | Loop truncates the first non-fitting section THEN BREAKS — survival set is incidental, not structural: a 10k-char objective description at the 1,200-char floor consumes the whole budget and active_work never renders. The §111 guarantee holds only for short objectives | LIVE, flagged → designed fix: per-section minimum reservations instead of break-on-first |
| Budget honesty | Char-exact accounting covers EVIDENCE ONLY — system prompt, user message, tool schemas, and up to 5 tool results × 8,000 chars sit outside the negotiated budget (usage telemetry is post-hoc) | LIVE, flagged |
| Artifact inclusion | Filename + sha256[:12] + bytes + source — never file bytes; metadata-only | LIVE, clean |
| Environment inclusion | Interface kind only; `environment.active_work_count` collected but never rendered | LIVE (stub-ish but honest) |
| Active work inclusion | ≤5 items, metadata only (capability, status, wake, attempts) — never payload inputs | LIVE, clean |
| Memory inclusion | ≤8 budgeted evidence lines (see memory-deep-audit Q13) | LIVE |
| Conversation inclusion | The "conversation" section is a recency timestamp; ZERO actual turns delivered — past exchanges reach the model only as ≤8 memory lines. `set_summary` has no callers | LIVE-but-hollow (documented) |
| Objective inclusion | **Was dead** — `ConversationRecord.objective_id` never written in any production flow (the section could never fire); **FIXED `67357e0`**: the bridge writes the link | FIXED |

## The key question — would a completely different objective receive useful context?

After the CV-6 fix: yes, structurally. The assembler is objective-agnostic — it emits WHAT IS TRUE (objective text, outstanding work metadata, artifact metadata, budgeted memory, recency) with no domain vocabulary anywhere (grep-verified). A lawyer, doctor, or engineer resuming an objective receives the same evidence shape as a student. The honest caveats: (1) conversation turns are absent (memory summaries carry history); (2) degradation survival is incidental for oversized objectives; (3) evidence-only budgeting under-counts tool schemas.

## Remediation queue (mechanism-shaped, next cycles)

1. Structural degradation: reserve minimums per priority tier (objective + active work always render).
2. Whole-input budgeting: fold tool schemas + system orientation into the negotiated budget.
3. Conversation turns: wire `set_summary` or deliver bounded recent turns as evidence (mission §10 lists RECENT INTERACTION as a first-class input).
4. Render `environment.active_work_count` or stop collecting it.
