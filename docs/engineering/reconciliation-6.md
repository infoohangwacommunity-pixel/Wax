# Reconciliation #6 — Phases 1–12 master mission (sixth principal-engineer pass)

Basis: repository at `4f182d7` (verified independently: HEAD == origin/main,
clean tree, 585 tests passing, live probe PASS; the mission's hash
discrepancy resolved — `f063fa5` is the parent of `4f182d7`).

The founder-uploaded **Phases 1–12 master mission** was reconciled against
every module of the repository. Full classification in
`docs/engineering/worklog.md` (PASS-7-CYCLE3 entry). Foundational gaps
found and CLOSED this pass, each with tests + ADR + probe:

| # | Gap (mission §) | Verdict | Implementation + proof |
|---|---|---|---|
| 1 | Objective lifecycle incomplete (§15–17, §52, §99, §101) | BUILD | ADR-0020: waiting/awaiting_human/cancelled states; append-only execution history (objective ≠ execution, migration `b9c1d3e5f7a2`); runtime-owned evidence syncs (scheduled work → waiting, woken/consumed → active, pending approval → awaiting_human, last-work death → failed) with DB-fresh reads; `objective.list` / `objective.resume` / `objective.update_status` capabilities (evidence REQUIRED, terminal immutable). 16 tests |
| 2 | Outbound delivery not recoverable state (§55) | BUILD | ADR-0021: `delivery_records` (migration `c4d6e8f0a2b3`) + DeliveryQueue — pending/retrying/delivered/failed with exponential backoff and a deliverability horizon; maintenance pass retries (leader-elected, services-aware); bridge reply send-failure now enqueues a recoverable record instead of a dead-letter row nobody re-drove. 5 tests over the real ASGI app |
| 3 | No typed memory relationships (Phase 3, §6.3) | BUILD | ADR-0022: `memory_links` (migration `d6f8a2b4c9e1`) — supports/contradicts/derived_from/related_to, principal-scoped, idempotent, provenance; retrieval traverses one hop with damped scores; `memory.link` capability; consolidation writes derived_from edges BEFORE superseding; `importance` weights rank (cannot fabricate relevance); `observed_at` drives recency. 14 tests |
| 4 | Context missing semantic sections (Phase 5, §56, §99, §111) | BUILD | ADR-0023: `artifacts` table (migration `e1a3c5e7b9d2`, first-class records born at the acquisition boundary); ContinuityContext + assembler emit ACTIVE_WORK / ARTIFACTS / ENVIRONMENT with the mission's priority shape; degraded budgets keep objective + active work. 5 tests |
| 5 | No provider failover (§33–34, Scenario 8) | BUILD | ADR-0024: ordered candidate list from configuration; per-candidate retry+breaker; boot-time loud misconfiguration; honest last-error raise; response records the serving provider. 5 tests |
| 6 | No dedicated memory evaluation (Phase 4) | BUILD | `tests/evaluation/test_memory_evaluation.py` — the mission's ten dimensions (recall, irrelevance, knowledge update, temporal, contradiction, abstention, privacy, forgetting, consolidation, poisoning) against the real mechanisms. 10 tests |
| 7 | Evaluation suite found a real retrieval bug | FIX | recall arm matched substrings while the rank arm required exact tokens — recalled candidates scored zero and vanished. BM25 scoring is now prefix-aware (lightweight stemming); the suite asserts update/temporal scenarios pass end-to-end |
| 8 | OCR test incomplete skip gate | FIX | gates on BOTH the tesseract binary and Pillow (a Pillow-less host failed instead of skipping) |

Defects found BY the new mechanisms during implementation (fixed, tests
assert the honest behavior):

- Cross-session identity-map staleness let an interaction close a
  waiting objective (the live-path test caught it; every transition now
  re-reads DB state).
- `memory.store` links silently skipped refused targets; consolidation
  created edges after superseding (they could never exist). Both are
  now loud, verify-before-create behaviors.

## Test counts

- Session start (verified): 585
- After this pass: **640** (+55: objective lifecycle 16, delivery 5,
  memory links 14, context sections 5, failover 5, evaluation 10)
- Live probe: PASS after every batch; alembic chain head `e1a3c5e7b9d2`,
  fresh/upgrade/rollback discipline green.

## Deliberately remaining (evidence-based)

- Embedding retrieval (ADR-0019 non-goal stands: links + BM25 resolve
  the named failures at zero new infrastructure).
- MicroVM isolation tier, audio transcription, Anthropic offline
  tokenizer (ADR-0016/0018/reconciliation-5 non-goals re-affirmed).
- `code.run` output artifacts: runs do not enumerate produced files yet;
  recording a fake enumeration would be dishonest (ADR-0023 non-goal).
- Streaming failover: interleaving provider semantics mid-stream is not
  honestly composable; primary-only, documented (ADR-0024).
- **Pushes are blocked**: no GitHub credentials exist in this
  environment (the token used by earlier cycles was never persisted
  here and must be rotated anyway — it was exposed in chat). Commits
  `48b56cd`, `c3c7b99`, `3310ebb`, and the Batch E/F commit are local;
  the working tree is clean and history is linear. Push immediately a
  fresh token is provided.
