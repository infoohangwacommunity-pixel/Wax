# Memory Deep Audit — Constitutional Audit

Traced reality across `src/wax/memory/`, `state/memory_models.py`, capability impls, `continuity/`, bridge, migrations, ADR-0010/0012/0019/0022, and both memory test suites.

## The 20 questions, answered with evidence

1. **How is memory written?** Two doors: (a) the bridge writes ONE episodic record per exchange (`bridge/service.py:375-389`, user_message+assistant_response, truncated) — no formation policy, no TTL: store-everything-forever by design, retired only by consolidation or explicit forget; (b) the AI's `memory.store` v1.2.0 — gated, principal-scoped, verify-before-create for supersedes/links. No education/subject logic anywhere in either door.
2. **How is memory searched?** Two-stage recall (`repository.py:469-595`): newest-200 pool ∪ ILIKE substring hits on summary/content for the top-8 longest query terms → Python BM25 ranking with prefix-aware stemming. One real bug was found and fixed during this arc (recall/rank token mismatch). ⚠ The Postgres tsvector arm (migration `d9e4f2a8b1c7` + GIN index) has ZERO callers — classified DOCUMENTATION-ONLY; ADR-0019's "production recall" claim is corrected.
3. **How is memory ranked?** `score = bm25_norm × (0.7 + 0.3×recency) + 0.1×confidence + 0.1×(importance−0.5)×bm25_norm`; recency decays `exp(−age_days/14)` from `observed_at or created_at`. Importance cannot fabricate relevance (multiplied by bm25_norm). Confidence and importance are genuinely consulted — but usually NULL in practice (bridge rows never set them), and never surfaced in output.
4. **How is memory promoted?** Exclusively AI-driven `memory.consolidate` (≤20 sources → one durable record); no automatic consolidation (correct per ADR-0012:95). One flat table: "durable" is a kind change, not a separate store — honest and documented; no hidden episodic/durable split pretending to exist.
5. **How is memory forgotten?** Status flip to `forgotten` + exclusion from list/search/link endpoints — truly unavailable to retrieval and to the model. The row is retained (audit rationale documented). ⚠ For AI-stored sensitive data this is a standing retention exposure → founder question #2.
6. **How is memory revised?** Supersession = conditional UPDATE (only active→superseded, sets `superseded_by`); old evidence retained; chains work (A→B→C all leave retrieval). Post-fix: a supersede that lands on zero rows is a loud in-transaction error, not a false `"superseded": true` response.
7. **Contradictions?** Two conflicting memories CAN both be active — by design (resolution belongs to the intelligence via supersedes, ADR-0012:57-59; asserted by `test_contradiction_links_record_the_conflict`). ⚠ BUT the `contradicts` link kind is traversed identically to `supports` (damped 0.6×) and never rendered in context — the conflict vocabulary exists; its meaning reaches no consumer. Classified IMPLEMENTED-BUT-UNWIRED; rendering conflict markers in evidence is the designed next step.
8. **Relationships?** `memory_links`: supports/contradicts/derived_from/related_to; principal-scoped; unique per (from,to,kind); provenance; idempotent; verify-BEFORE-create (refused link = loud error). Supersession deliberately NOT a link kind.
9. **Provenance?** Every record: NOT NULL `provenance` + optional `source_execution_id`. ⚠ Writer-identity constants though: `memory.store` hardcodes `provenance="model_observation"` (the AI cannot record "the user stated this"), and bridge rows are labeled `user_statement` while content is half assistant-authored, with `source_execution_id` NULL despite execution in scope (ADR-0010's traceability claim was false for bridge rows — flagged).
10. **Confidence?** Weights the rank; almost always NULL; never surfaced in output. Live-but-nearly-unexercised.
11. **observed_at vs created_at?** Half-wired: the ranker reads `observed_at`, but the recency pool orders by `created_at`, the context re-sort uses `created_at`, and `memory.search` output exposes only `created_at` — the layer that must do temporal reasoning never SEES observation time. Wired fully is the designed remediation.
12. **Consolidation?** derived_from edges written to EVERY source BEFORE superseding (ordering bug fixed during this arc); non-destructive; sources retained; rollback = reading superseded sources.
13. **How does context consume memory?** Recency pool (newest 5) ∪ relevance pool (top 5 vs current message), deduped, capped at 8; each emitted as `[evidence: memory (reason)] summary` at priority 3, budgeted, truncation announced. Memory is evidence, not recipe — the strongest part of the subsystem.
14. **How many memories visible?** ≤8 per context build; search limit 5 (descriptor max 20). Hardcoded pool sizes documented in hardcoding-inventory.md.
15. **Which memories never become visible?** Anything outside the newest-200 whose wording does not substring-match a top-8 query term is unreachable (link expansion only fans out from an existing hit). English-biased tokenization makes non-whitespace-script recall ≈ exact-substring. ADR-0019 names the hole; the fix narrows it (paraphrase), does not close it. Classified honest limitation, re-evaluate when links+BM25 demonstrably fail a mission dimension.
16. **Could two conflicting memories both survive as current?** Yes (Q7) — constitutionally intended; the model resolves.
17. **User changes their mind?** AI (or bridge evidence) stores a correction with `supersedes` → old record superseded (retained, excluded), new record active. Works end-to-end (evaluation suite asserts the update scenario).
18. **Silent failures found:** maintenance loop swallows all exceptions (log-only); supersede rowcount ignored (FIXED `f44a067`); malformed memory dicts silently skipped in assembly; zero-score candidates silently vanish (honest abstention, but indistinguishable from "pool too small"); `memory.link` repo returns None on validation failure (loud at capability layer, silent to internal callers).
19. **Hidden assumptions:** English-biased tokenization/stopwords/stemming in a supposedly universal runtime; BM25 indexes JSON key names (a query token matching a key like "interface" matches every row of that writer shape — IDF dampens, never eliminates). No education/subject/tutor assumptions found.
20. **Memory as instructions?** No — memory enters context only as budgeted, labelled `[evidence: …]` lines. ⚠ But evidence lines ride the SYSTEM message role (`bridge/service.py:1051-1052`): untrusted memory text on the channel models treat as instruction authority, separated only by a text label. Classified flagged-risk (not yet remediated): the boundary should become structural (data role), not lexical. Tracked in violations follow-ups.

## False mechanisms eliminated or flagged

- `memory.read/write` enforcement theater — FIXED (CV-3).
- `memory.forget idempotent=True` lie — FIXED (CV-9).
- Postgres FTS "production" claim — corrected to DOCUMENTATION-ONLY.
- `sensitivity` behavioral claim — replaced with honest RESERVED (CV-8).
- ADR-0010 "every stored fact traceable to the execution that claimed it" — false for bridge rows; flagged.

## Verdict

The memory spine honors the constitution: flat principal-scoped records, provenance columns, explicit supersession, runtime-owned forgetting, evidence-not-recipe delivery, zero domain contamination. Its failures were honesty failures — surfaces claiming more than they do — now corrected or honestly flagged. Remediation queue: structural data-role for evidence lines; observed_at surfaced to the model; contradicts rendered as conflict; provenance made an input of memory.store; bridge rows linked to their execution.

---

## POST-OMEGA re-audit (this cycle)

An independent read-only re-audit of the full memory substrate against
the POST-OMEGA mission §10–§16 confirmed the prior fixes hold (CV-3
permission enforcement, CV-8 sensitivity RESERVED, CV-9 forget
contract) and that the substrate is mechanisms-only: no domain
assumptions, no hardcoded scheduling, forgotten memories excluded from
every retrieval path, retention still a founder boundary (ADR-0025).

Findings and dispositions:

| ID | Classification | Disposition |
|---|---|---|
| MEM-1 retrieval docstring claimed a `use_postgres_fts` path that does not exist (tsvector/GIN columns have zero application readers) | FALSE-MECHANISM CLAIM | **FIXED this cycle**: docstrings corrected to state the single portable retrieval path; the indexed columns are honestly marked reserved-for-future |
| MEM-2 untrusted memory text delivered as `MessageRole.SYSTEM` lines (lexical `[evidence: …]` label only) | CARRIED FLAGGED-RISK | OPEN — designed follow-up: structural data-role boundary for evidence (prompt-architecture scope, own cycle) |
| MEM-3 `contradicts` edges traversed identically to `supports`, never rendered as conflict markers | IMPLEMENTED-BUT-UNWIRED | OPEN — designed follow-up: conflict-marker rendering in evidence assembly |
| MEM-4 `observed_at` drives ranking but is not surfaced in search/context outputs; recency pool sorts by `created_at` | IMPLEMENTED-BUT-UNWIRED | OPEN — designed follow-up: observation-time surfacing + ordering |
| MEM-5 bridge episodic rows: `provenance="user_statement"` for mixed content, `source_execution_id` NULL; `memory.store` hardcodes provenance | CARRIED PROVENANCE GAP | OPEN — designed follow-up: execution-id provenance + model-declarable provenance (validated) |
| MEM-6 dead `MemoryRead` contract (never instantiated, incomplete fields) | DEAD | **FIXED this cycle**: deleted, including re-exports |
| MEM-7 `unlink()` has no production/capability caller | TEST-ONLY | **MARKED HONESTLY this cycle**: docstring states it is an internal primitive with no capability surface, deliberately kept |
| MEM-8 doc rot: "future MemoryService" claim, "archive" state in `expire_due` docstring, "archived neighbors" in ADR-0022 | DOC-ROT | **FIXED this cycle**: all three surfaces state present reality (no archive state exists; compression absent and unclaimed) |
| MEM-9 `memory.consolidate` silently omitted sources it failed to supersede | MINOR HONESTY | **FIXED this cycle**: partial supersession now reported loudly (`supersede_refused_ids`) |
| MEM-10 founder decision map cites "ADR-0024 memory evaluation"; repo ADR-0024 is provider failover | DOC DISCREPANCY | Noted — no repo change; the evaluation suite is evidence-harness TEST-ONLY testing real machinery |
| Declared output schemas omitted fields the impls actually return (`linked`, `already_forgotten`) | CONTRACT DRIFT | **FIXED this cycle**: schemas completed |

Open behavioral follow-ups (MEM-2/3/4/5) are designed follow-ups, not
violations: they change prompt-assembly and output shapes and deserve
their own verification cycle.
