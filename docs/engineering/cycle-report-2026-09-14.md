# Cycle Report — 2026-09-14

**Principal Engineer Pass 7, Cycle 3 — delivery of the Phases 1–12 mission work**

This report is the durable record of what was implemented, verified, and pushed
in this cycle. It is written so that any future engineer (human or agent) can
audit the claims against the repository itself: every number below is
reproducible with the commands shown.

Governing directive: `docs/mission/phases-1-12-mission.md` (the founder's
Phases 1–12 master mission, preserved verbatim in this repository).
Governing philosophy: **"Infrastructure, not intelligence."**

---

## 1. State found at session start

Verified independently before any work (never trusting the prompt or the
previous agent's report):

| Check | Result |
|---|---|
| Local `main` | `f7e71b7` |
| `origin/main` (before push) | `4f182d7` |
| Divergence | local strictly ahead by 5 commits, zero behind — clean fast-forward |
| Working tree | clean |
| Test suite at `4f182d7` | 585 passed (previously verified) |
| Live probe at `4f182d7` | PASS (real HTTP path) |
| Why the 5 commits were local-only | the previous session's GitHub token was revoked; pushes were blocked, tree kept clean, history kept linear |

---

## 2. What was implemented this cycle (the 5 pushed commits)

Each batch follows the mission's execution protocol: reconnaissance → design
(ADR) → implementation → integration → tests → live probe → migration
discipline → commit. All migrations follow the established fresh-database /
upgrade-from-production / downgrade-considered discipline.

### 2.1 `48b56cd` — Objective lifecycle: evidence-driven completion (ADR-0020)

Mission sections §15–17, §52, §99, §101 (Phase 6: objective lifecycle;
Phase 9: task performance).

- **States**: `waiting`, `awaiting_human`, `cancelled` added to the objective
  state machine; terminal states are immutable.
- **Objective ≠ execution**: new append-only `objective_executions` history
  table (migration `b9c1d3e5f7a2`). One objective can produce many executions,
  retries, resumptions — the mission's §16 shape.
- **Runtime-owned evidence syncs** (`wax.objective.evidence`): the *runtime*,
  not the model, drives state from durable evidence — work scheduled →
  `waiting`; work woken/consumed → `active`; approval pending →
  `awaiting_human`; last work death → `failed`. Completion requires actual
  evidence (mission §24: never mark done because the model said "Done.").
- **Capabilities**: `objective.list`, `objective.resume`, `objective.update_status`
  behind the standard gate chain with `objective.read`/`objective.write`
  permissions (member+admin; the AI principal stays at zero — agency without
  sovereignty).
- **Defect found by the live-path test and fixed**: cross-session identity-map
  staleness let an interaction close a *waiting* objective through a stale
  in-memory copy. Every transition now re-reads DB state. This is exactly the
  class of bug mission §121 warns about.
- **Tests**: 16 (`tests/integration/test_objective_lifecycle.py`).

### 2.2 `c3c7b99` — Durable outbound delivery lifecycle (ADR-0021)

Mission §55, §61–62, §65, §92 (interface delivery must become durable; no
silent failure).

- **New `delivery_records` table** (migration `c4d6e8f0a2b3`) + `DeliveryQueue`:
  `pending → retrying → delivered | failed` with exponential backoff, a
  deliverability horizon, and honest exhaustion.
- **Maintenance pass** gained the leader-elected, services-aware delivery-retry
  sweep (skips honestly when the live container is unavailable).
- **Bridge reply send-failure** now enqueues a recoverable record with the
  first attempt already spent — replacing the previous dead-letter row that
  nobody re-drove (the exact "silently swallowed delivery failure" the mission
  forbids). Execution result and delivery result stay separate state.
- **Tests**: 5 (`tests/integration/test_delivery_lifecycle.py`) including the
  full failure → maintenance-retry → delivered arc over the real ASGI app.

### 2.3 `3310ebb` — Typed memory relationships + importance + observation time (ADR-0022)

Mission Phase 3 (memory graph/relationships), §6.3 (memory metadata), §49.

- **`memory_links` table** (migration `d6f8a2b4c9e1`): typed edges
  `supports / contradicts / derived_from / related_to`; principal-scoped;
  idempotent per `(from, to, kind)`; every edge carries provenance of the link
  decision. Supersession deliberately remains a lifecycle column, not a link
  kind (one mechanism, one meaning).
- **`importance`** (rank weight; NULL = neutral) and **`observed_at`**
  (observation time, distinct from write time) on memory records. Recency in
  the rank now reads `observed_at` — temporal reasoning per mission §4.3.
- **Retrieval**: `search_relevant` performs one-hop linked-neighbor expansion
  (top-3 anchors, ≤5 neighbors each, scores damped 0.6×, lifecycle-respected).
  "Which memories matter for this objective" becomes traversable *without*
  embeddings — consistent with ADR-0019's evidence-based non-goal.
- **Capabilities**: `memory.store` v1.2.0 accepts importance/observed_at/links
  with **verify-before-create** (a refused link is a loud error, not a silent
  skip); new `memory.link` capability (idempotent, honest `existed` reporting).
  `memory.consolidate` writes `derived_from` edges to every source *before*
  superseding — the previous ordering produced edges that could never exist.
- **Two fake-mechanism patterns eliminated during implementation**: silent link
  skip; doomed edge ordering. Both are now loud, verified behaviors with tests
  asserting the honest failure.
- **Tests**: 14 (`tests/integration/test_memory_links.py`).

### 2.4 `d9d6790` — Context semantic sections + first-class artifact records (ADR-0023)

Mission Phase 5 (context engine), §11 (semantic sections), §12 (priority), §56
(artifacts first-class), §111 (context degradation).

- **`artifacts` table** (migration `e1a3c5e7b9d2`): owner, workspace link,
  workspace-relative path (no host-path leakage), sha256, size, source,
  execution provenance, TTL mirroring the workspace. Records are **born at the
  acquisition boundary** where integrity is computed; `workspace.acquire` now
  returns `artifact_id`. Artifacts are no longer arbitrary filesystem paths.
- **ContinuityContext** gained `active_work`, `recent_artifacts`,
  `environment`; the ContinuityService fetches outstanding work (metadata
  only — capability, status, wake, attempts; never payload contents) and
  newest artifacts (filename + integrity **prefix** + size; never bytes).
- **Assembler** emits the labelled sections with the mission's priority shape:
  `objective(0) > active_work(1) > conversation(2) > memory(3) > artifacts(4)
  > environment(5)`. New conversations see outstanding work too — "keep working
  while I am away" survives the conversation boundary (mission §17).
- **Degraded budgets** keep objective + active work and discard lower-priority
  material — asserted by test (mission §111).
- **Tests**: 5 (`tests/integration/test_context_sections.py`).

### 2.5 `f7e71b7` — Provider failover + memory evaluation suite (ADR-0024)

Mission §33–34 (multi-model routing, model failure), Scenario 8, Phase 4
(memory evaluation).

- **IntelligenceService failover**: ordered candidate list from
  `WAX_LLM_PROVIDER_FALLBACKS`; per-candidate retry + circuit breaker;
  boot-time loud misconfiguration; duplicate skipping; honest last-error raise;
  the response records which provider actually served the call; `close()`
  closes all candidates. The objective survives provider failure (mission §64).
  Streaming failover is a documented non-goal: interleaving provider semantics
  mid-stream is not honestly composable.
- **Memory evaluation suite** (`tests/evaluation/test_memory_evaluation.py`):
  the mission's ten memory dimensions as scenario tests — recall, irrelevance,
  knowledge update, temporal reasoning, contradiction, abstention, privacy,
  forgetting, consolidation, retrieval poisoning — against the real mechanisms.
- **The evaluation suite found a real retrieval bug**: the recall arm matched
  substrings while the rank arm required exact tokens, so recalled candidates
  scored zero and silently vanished ("study" vs "studying"). BM25 scoring is
  now prefix-aware (lightweight stemming, zero new dependencies); the
  update/temporal scenarios assert the end-to-end behavior. This is the
  evaluation system doing its job (mission §42).
- **OCR test skip gate fixed**: now gates on *both* the tesseract binary and
  Pillow (a Pillow-less host previously failed instead of skipping).
- **Tests**: 5 failover + 10 evaluation.

---

## 3. Verification evidence (reproducible)

```
$ python -m pytest tests/
640 passed in 37.05s

$ python scripts/live_probe.py
PROBE PASS: live path verified over real HTTP   (exit 0)

$ alembic heads
e1a3c5e7b9d2 (head)   # fresh-DB + upgrade-from-production + rollback discipline green

$ GIT_ASKPASS=... git push origin main
4f182d7..f7e71b7  main -> main

$ git rev-parse main origin/main
f7e71b763fadfe56a8af2d9f61cc3b37d57216c4   # local
f7e71b763fadfe56a8af2d9f61cc3b37d57216c4   # origin — parity verified, tree clean
```

**Test progression this cycle: 585 → 606 → 620 → 625 → 640 (+55)**
(objective lifecycle 16, delivery 5, memory links 14, context sections 5,
failover 5, memory evaluation 10). Live probe was run PASS after *every*
batch, not only at the end.

---

## 4. How this cycle maps to the 12-phase mission

| Mission phase | Delivered by |
|---|---|
| Phase 6 — objective lifecycle | ADR-0020 |
| Phase 9 — task performance / evidence-based completion | ADR-0020 |
| §55/§92/§65 — durable delivery, no silent failure | ADR-0021 |
| Phase 3 — memory graph / relationships | ADR-0022 |
| Phase 4 — memory evaluation | evaluation suite (ADR-0024 commit) |
| Phase 5 — context engine semantic sections | ADR-0023 |
| §56 — first-class artifacts | ADR-0023 |
| Phase 11 — model independence / provider failure | ADR-0024 |
| §42–45 — evaluation discipline | memory evaluation suite; it found and fixed a real retrieval bug |

Earlier cycles in this same pass (already on `origin/main` before this push)
delivered: namespace sandbox isolation (ADR-0016), maintenance leader election
(ADR-0017), tokenizer-exact context accounting inside adapters (ADR-0018),
two-stage memory retrieval (ADR-0019), multi-worker runtime (ADR-0013), human
approval primitive (ADR-0014), acquisition/retention/context negotiation
(ADR-0015), durable waiting (ADR-0011), memory & context mechanisms (ADR-0012).

---

## 5. Deliberately remaining (evidence-based, not convenience non-goals)

- **Embedding retrieval** — ADR-0019 non-goal stands: typed links + BM25
  resolve the named retrieval failures with zero new infrastructure.
  Re-evaluate when lexical+link recall demonstrably fails a mission dimension.
- **MicroVM isolation tier** — the namespace sandbox (ADR-0016) is the honest
  strongest local boundary; a hardened multi-tenant tier is a deployment
  upgrade, documented in ADR-0016 and the mission's own §82 terms.
- **`code.run` output artifacts** — runs do not yet enumerate produced files;
  recording a fake enumeration would be dishonest (ADR-0023 non-goal). The
  acquisition boundary records what integrity can actually verify.
- **Streaming failover** — primary-only, documented (ADR-0024).
- **Anthropic offline tokenizer, audio transcription** — ADR-0018 /
  reconciliation-5 non-goals re-affirmed (external/toolchain constraints).

These are external, deployment-dependent, or intentionally deferred with
evidence — not missing universal mechanisms mislabeled as non-goals.

---

## 6. Security notes

- The GitHub token used for this push was supplied fresh this session and is
  **not stored in the repository, the worklog, or any committed file**.
  It was previously exposed in chat history: **revoke it now that the push is
  done** and prefer a scoped, short-lived credential for the next cycle.
- No secrets were found in the mission document before preserving it in-repo
  (checked for token patterns).
- Capability permissions remain: the AI principal holds zero
  `objective.write`-class authority; human approval stays outside AI
  sovereignty (mission §39, §83).
