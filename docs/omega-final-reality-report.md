# WAX Final Reality Report (OMEGA)

The repository as it actually exists after the OMEGA cycle. Every claim
was verified against code, tests, migrations, and the live probe at the
commit recorded below — not against any previous report.

**Commit:** recorded per-push in git history (OMEGA cycle, post
`b439aae`). **Branch:** `main`, working tree clean, HEAD == origin/main
at each pushed batch. **Verification at audit time:** 655 tests passed
(0 failed), 13/13 live-probe checks PASS over real HTTP, migration head
`f2b4d6a8c0e2` (fresh-db and upgrade validated by tests).

## 1. Architecture — what exists

A Python runtime (`wax`, 122 source files, ~20,300 LOC, 19 database
tables, 27 ADRs) organized as: `core` (config/exceptions/invariants),
`identity`, `memory`, `continuity` (context), `objective`, `agency` +
`authority` (gates + approvals), `capabilities` (registry + invoker),
`execution`, `runtime` (bridge, work, delivery, maintenance,
leadership, provisioning, lifecycle), `intelligence` (providers +
failover), `interfaces` (whatsapp adapter), `isolation`, `security`,
`media`, `resources`, `reliability`, `observability`, `state`
(models + engine).

The philosophical shape: **infrastructure, not intelligence**. The
runtime owns identity, ownership, authorization, isolation, budgets,
persistence, durability, and delivery. The intelligence chooses what to
do; every effect passes a gate; every claim needs evidence.

## 2. Runtime graph — what actually executes

```
interface adapter (WhatsApp)
  → webhook (HMAC-verified) → bridge.process()
    → idempotency (unique constraint; conditional retry claim)
    → identity resolution (credential → principal; first contact seeds member role)
    → security gate (rate limit → cost cap → abuse/injection)
    → DURABLE ACCEPTANCE: objective + execution + dedup record committed
      → signal ledger: interface.message:<principal>
    → context assembly (7 priority sections, budget negotiated from provider limit)
    → intelligence loop (provider failover chain; bounded tool rounds)
        → per tool call: SHARED GATE (agency → approval → budget → authority → invoker)
        → execution steps recorded per attempt
    → episodic memory of the exchange
    → honest completion (succeeded only if no outstanding durable work)
    → response via adapter; failed sends → durable DeliveryRecord
durable work: work.schedule → lease claim (SKIP LOCKED) → SAME SHARED GATE
  → evidence sync → retry/backoff/dead-letter/requeue
maintenance (leader-only): approval expiry, signal retention, delivery
  retries, conversation lifecycle; reapers (provisioning, memory) leader-guarded
```

## 3. Memory — exactly what it can and cannot do

CAN: per-principal typed records (6 kinds) with confidence, importance,
provenance, observed_at, optional expiry; relevance retrieval (BM25 +
recency + confidence + importance) with bounded one-hop typed links
(supports / contradicts / derived_from / related_to); supersession
preserving history; consolidation preserving provenance; soft
forgetting (explicit + TTL reaper) excluded from every retrieval path.
CANNOT: hard-delete (no erasure path — ADR-0025); share across
principals; enforce `sensitivity` (RESERVED until a privacy policy);
auto-consolidate. See `docs/omega-memory-architecture.md`.

## 4. Context — exactly what reaches the model

One minimal system prompt + labelled evidence lines (OBJECTIVE[0],
ACTIVE_WORK[1], CONVERSATION[2], MEMORY[3], ARTIFACTS[4],
ENVIRONMENT[5]) + the user message (untrusted content sanitizer-
marked) + tool specs for AVAILABLE capabilities. Budget negotiated
from the provider's advertised limit. Objective evidence cannot be
crowded out by conversation (priority 0 vs 2). See
`docs/omega-context-architecture.md`.

## 5. Objectives — exactly how continuity works

Every message creates an objective + execution (durable acceptance
committed BEFORE intelligence). Terminal `succeeded` requires zero
outstanding durable work in the DB. Waiting states are evidence-driven:
scheduled work → waiting; pending approval → awaiting_human; approval
consumed → active. History (`objective_executions`) records every
participation. See `docs/omega-agent-runtime.md`.

## 6. Agent loop — exactly how reasoning becomes execution

Bounded rounds of: LLM call (budget-metered) → tool calls through the
shared gate → results as tool messages → final text (or the honest
"could not complete within the allowed rounds"). Durable work repeats
the same gate chain at wake time. Loop safety: iteration budgets,
resource budgets, timeouts, backoff caps, dead-letters, approval gates,
checkpoints, orphan recovery.

## 7. Capabilities — exactly what can be discovered, requested, authorized, executed

21 registered capabilities (list in the open-world document). Discovery
LIVE (tool specs every turn); extension NOT (closed registry by design;
ADR-0026). Invocation requires: availability, agency verdict, approval
when required (fingerprint-bound, expiring, exactly-once), budget,
DB-backed permission. Failures are structured and honest. All four
tracked §58 items are FIXED (`b439aae`): database-owned creation
idempotency, one shared gate, delivery failures as recoverable state,
background approvals notified or durably retried.

## 8. Environments — exactly what can be provisioned

`scratch.workspace`: ephemeral owned directory, TTL 10s–24h,
per-principal cap 5, leader-guarded reaper, audited. `code.run`:
namespace sandbox (no network, read-only root, masked /proc+/sys,
noexec private /tmp, rlimits, process-group kill) with LOUD fallback
to subprocess; executed grade reported. Media extractors (OCR, PDF)
exist and are tested but are NOT wired into the live path (Phase T).
Container/microVM/browser: declared, unimplemented, honest.

## 9. Interfaces — exactly what is interface-specific

Everything WhatsApp lives under `interfaces/whatsapp/` (adapter +
client + contracts) and the composition root: webhook verification,
message → RuntimeRequest shaping, delivery policy (Meta's 24h window)
declared by the adapter, sender registration. The core knows interface
KINDS, never vendor shapes (AST-tested). The approval decision grammar
(`/approve <id>` / `/deny <id>`) is generic and adoptable by any
adapter in one call.

## 10. Security — exactly what is enforced

See `docs/omega-security-model.md`. Highlights: the model cannot grant
itself anything (empty `ai` role; runtime-resolved identity);
approvals are replay-proof and database-owned; ownership is enforced at
query level everywhere; SSRF + HMAC + redaction enforced; untrusted
content is marked data. Honest boundaries: in-memory enforcement
perimeter (single-instance envelope), no principal dimension in
metrics, writer-convention (not check) on audit payloads, sensitivity
RESERVED.

## 11. Tests — exact current counts

**655 passed, 0 failed, 0 skipped** (~40s): unit + integration +
evaluation (10 memory/context dimensions) + architecture (7 boundary
tests) + migrations (fresh-db + upgrade + downgrade validation).

## 12. Live probes — exact results

`scripts/live_probe.py` over real HTTP against a live app + real
WhatsApp-compatible endpoint: **13/13 PASS** — healthz, readyz,
metrics, webhook verification, signed webhook, processed-message
metric, signal ledger record, durable event-wake work, pending approval
created (destructive), human decision over the live webhook grammar,
exactly-once approved execution, ledger retention pruning, bad
signature rejected. (The probe also demonstrates the CV-14 fix live:
the approval notification attempt fires through the real delivery
path and lands as durable retry state when the vendor returns 401.)

## 13. Migrations — exact head and validation

Head: **`f2b4d6a8c0e2`** (approval creation idempotency: partial unique
index `uq_pending_approvals_principal_fp_pending`; downgrade drops
exactly that index). Chain: 12 prior revisions ending `e1a3c5e7b9d2`,
each with a real downgrade path; fresh-db and production-upgrade
validation covered by `test_migrations.py`.

## 14. Unwired mechanisms (honest list)

- Media pipeline (OCR/PDF extraction): implemented, tested, NOT on the
  live path (Phase T pending).
- `release()` / `promote()` on provisioned workspaces: implemented, no
  production caller, no capability surface.
- Capability `set_status` / `unregister` / `REVOKED`: implemented, no
  production caller.
- `soft_delete_principal`: implemented, unwired (account-deletion
  semantics are a founder decision — ADR-0025).
- Dead-letter reprocessing worker + `reprocessed` flag: RESERVED,
  no worker exists.
- `MemoryStatus` has no `archived` state by design (removed this cycle
  — no writer ever produced it).

## 15. Deferred mechanisms (with reasons)

- Open capability registry (ADR-0026): gates must exist before
  openness; build order recorded; nothing justified implementing yet.
- Shared enforcement store (rate/cost/abuse) + execution heartbeat
  (ADR-0027): required before multi-instance scale-out; single-instance
  is the current honest envelope.
- Streaming failover: provider semantics interleave mid-stream;
  documented rather than faked (ADR-0024).
- Retention pruners: founder policy must land first (ADR-0025).

## 16. Philosophy violations remaining

**None open.** 14 corrected across the two audit cycles (CV-1..CV-14,
register in `docs/constitutional-audit/constitutional-violations.md`).
The §58 four tracked items are fixed in code with tests, not closed by
documentation.

## 17. Known limitations (do not hide)

1. Database growth until the founder retention policy lands (18 of 20
   tables unbounded — documented, deliberate).
2. Enforcement perimeter is single-instance (ADR-0027).
3. Output schemas are declared but not validated; `idempotency_key` is
   accepted but not honored (ADR-0026 primitive list).
4. Bridge executions have no heartbeat; rolling multi-instance deploys
   would fail each other's >15-minute executions (designed fix
   deferred, ADR-0027).
5. Media extraction, account deletion, and dead-letter re-drive are
   built or designed but not live paths.
6. Evaluation is deterministic/mocked by design; no live-LLM harness.

## 18. Final acceptance (mission §82, evidence-backed)

1. Education disappears → architecture stands (zero domain vocabulary
   in the core; open-world scenarios run unknown domains). 2. WhatsApp
   disappears → core stands (AST boundary tests; interface policy is
   adapter-declared). 3. Provider disappears → failover chain +
   per-candidate breakers (ADR-0024). 4. Unpredicted request → generic
   primitives (21 capabilities, durable work, signals, artifacts). 5.
   Month-long absence → context reconstructs from durable state. 6–7.
   False/contradictory memory → supersession + contradict links with
   evidence preserved (evaluation suite). 8–9. Five workers or none →
   intelligence composes scheduled work; nothing spawns agents on its
   own. 10. Unknown capability → honest structured "no such
   capability". 11. Model grants permission → impossible (empty ai
   role; authority is DB-backed). 12. Model declares success → only
   runtime evidence closes states (DB guard). 13. Untrusted content →
   sanitizer-marked data; gates hold even under a compromised model. 14.
   Cross-principal access → refused at query level (tested). 15.
   Recovery → leases, fencing, idempotency, delivery retries
   (concurrency tests). 16. Why a memory entered context → section +
   score evidence. 17. Why an action was authorized → audit event per
   decision. 18. Why an objective changed state → evidence syncs +
   transition rules. 19. Unwired mechanisms → §14, complete. 20.
   Philosophical violations → §16, none open.
