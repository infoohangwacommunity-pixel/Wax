# Audit Response — Evidence Report

**Scope:** forensic-audit findings → disposition → implementation → proof
**Branch:** `main` (commits `97ff426..HEAD` are the audit-response work)
**Test baseline at audit:** 312 passing · **Current:** 373 passing, 0 failed

This report contains only what exists in the repository. Every claim names
its proof: a test file, a source path, or a live-path probe.

---

## 1. Disposition of audited systems

| Audited system | Audit verdict | Disposition | Proof |
|---|---|---|---|
| WhatsApp webhook GET verification | BROKEN (param names) | FIXED | `src/wax/runtime/app.py` (`hub.*` aliases); `tests/integration/test_webhook_http.py` |
| WhatsApp webhook POST binding | BROKEN (body as query param; signature header unbound) | FIXED | binding notes in `app.py`; `test_webhook_http.py` exercises real Meta-shaped POST with HMAC header |
| RuntimeBridge enforcement path | BYPASSED (services never built) | WIRED | `RuntimeServices.build(settings)` in lifespan (`app.py`); architecture tests forbid bypass |
| Capability Registry / Invoker | IMPLEMENTED-BUT-UNWIRED | WIRED | invoker invoked from live bridge path; `tests/integration/test_bridge.py` |
| Authority / Agency | IMPLEMENTED-BUT-UNWIRED | WIRED | every capability invocation passes Agency gate → Authority check; `tests/unit/` + integration |
| Resource Accountant | IMPLEMENTED-BUT-UNWIRED | WIRED | metering attributed per invocation; integration coverage |
| Security (rate limit, abuse, cost) | TEST-ONLY | WIRED into live path | `RuntimeServices` fields consumed by bridge; integration tests |
| Metrics / Observability | TEST-ONLY | WIRED | `RuntimeMetrics` emitted at path boundaries |
| Continuity (interrupted conversations) | IMPLEMENTED | WIRED + retained | resume-before-intake in bridge path |
| Reliability (retries, circuit breaker, dead-letter) | IMPLEMENTED | WIRED | failure path marks FAILED (see §4), terminal → dead-letter |
| SubprocessBoundary (code execution) | TEST-ONLY | WIRED as authority-gated `code.run` capability | `src/wax/capabilities/code_run.py`; ADR-0006 |
| Intelligence → capability invocation | MISSING | BUILT | tool-calling contract in intelligence layer; `tests/integration/test_intelligence.py` |
| Durable work (survives restart) | MISSING | BUILT | `migrations/versions/b7f21c9d4e02_phase_y_durable_work.py`; ADR-0004 |
| Dynamic provisioning (ephemeral resources) | MISSING | BUILT | ADR-0005 |
| WhatsApp length-aware delivery | MISSING (hard 4000-char rejection) | BUILT | `send_long_text` in client; `tests/integration/test_whatsapp_phase_w.py`; ADR-0007 |

## 2. Live-path bugs fixed (each verified, not just patched)

1. **POST `/webhooks/whatsapp` 422-rejected every genuine Meta POST** — a
   plain `bytes` body parameter was bound as a query parameter. Fix reads
   raw body via `request.body()`; signature bound via
   `Header(alias="X-Hub-Signature-256")`. Proof: HTTP-level test posts a
   real Meta-shaped payload with valid HMAC and reaches the bridge.
2. **GET verification ignored Meta's actual query names** — `hub.mode`,
   `hub.verify_token`, `hub.challenge` now bound by alias. Proof:
   HTTP-level test performs the full Meta verification handshake.
3. **Execution failure left work in RUNNING forever** — terminal failures
   now transition to FAILED, and terminal-failure work dead-letters with
   full context (no silent drops, no fake success). Proof: integration
   tests on the failure path.

## 3. New runtime mechanisms (composable, not domain features)

- **Durable work runtime** — persisted work items, wake conditions,
  leases, in-process worker, retries with backoff, dead-letter,
  identity continuity. A reminder is a *composition*: work item → wake →
  capability invocation → delivery. No ReminderService exists anywhere.
- **Dynamic provisioning** — ephemeral resources with owner, scope, TTL,
  limits, audit, enforced cleanup. The runtime returns real constraints
  on over-limit requests.
- **Tool-calling contract** — the intelligence layer requests
  capabilities by descriptor; the runtime gates, authorizes, meters,
  audits, and executes. The AI proposes; the runtime disposes.
- **Interface intelligence** — WhatsApp chunking, markers, and wire-limit
  ownership in the interface layer (ADR-0007).

## 4. Deliberately left unwired

- **Multi-process work workers** — the lease design permits it; the
  single-process loop is intentional for this deployment stage
  (Railway single instance). Documented in ADR-0004.
- **Media pipeline extensions** beyond the current extraction set —
  interface-level, added per demand, not speculatively.
- **No ReminderService / TimerService / domain modes** — permanently.
  This is not "unwired", it is *forbidden* (architecture tests enforce
  no domain concepts in `wax.core`, INV-01).

## 5. Proof of live execution

`scripts/live_probe.py` boots the real ASGI app (migrations applied), then
over real HTTP:

1. `GET /healthz` → 200.
2. Performs the Meta GET verification handshake → challenge echoed (200).
3. POSTs a signed, Meta-shaped text webhook → `{"status":"ok",
   "events_processed":1}`. Structured logs for this single request show the
   entire live chain executing in production: `identity.principal.created`
   → `authority.principal_role.assigned` → `objective.created` →
   `execution.created/started` → `conversation.created` →
   `resources.allocated` (budgets) → `intelligence.complete` →
   `execution.step.recorded` → `memory.record.created` →
   `execution.completed` → `objective.transitioned(succeeded)`.
   The reply send attempt hits the real Graph API and its real failure
   (probe has no valid Meta token) is logged as
   `whatsapp.response.send_failed` with the actual error — the runtime
   never fabricates success.
4. POSTs an invalid-signature payload → 200 with
   `{"status":"invalid_signature"}` and **no processing** (matches the
   committed HTTP-test contract: do not leak validity to probers, avoid
   Meta retry storms).
5. POSTs an oversize body → guard observed, server stays healthy.

## 6. Documentation produced

- `docs/decisions/ADR-0003` — wiring the unwired (RuntimeServices)
- `docs/decisions/ADR-0004` — durable work runtime
- `docs/decisions/ADR-0005` — dynamic provisioning
- `docs/decisions/ADR-0006` — code execution behind explicit authority
- `docs/decisions/ADR-0007` — interface intelligence
- `docs/engineering/worklog.md` — updated with audit-response stages
- This file (`docs/engineering/audit-response.md`)

## 7. Test inventory (373 passing)

- Unit: identity, authority, agency, capabilities, resources, security,
  reliability, continuity, state models, config, logging.
- Integration: webhook HTTP (both endpoints, real Meta shapes), bridge
  live path, intelligence tool-calling loop, durable work (incl. process
  restart and lease expiry), provisioning lifecycles, code.run gating,
  WhatsApp adapter/Phase-S/Phase-W delivery.
- Architecture: invariant enforcement (no I/O in core, no domain concepts
  in core, AI principal has no permissions, capability-only side effects).

## 8. Commit map (audit-response series)

| Commit | Content |
|---|---|
| `7e83dcb` | fix(runtime): Meta webhook parameter binding (live-path blockers) |
| `3a73131` | feat(runtime): wire unwired subsystems into the live path |
| `066cb1e` | feat(intelligence): tool-calling contract + gated invocation loop |
| `5c1cbfd` | feat(runtime): durable work runtime (Phase R/V) |
| `7898a34` | feat(runtime): dynamic provisioning (Phase S) |
| `b3ecb43` | feat(capabilities): code.run behind explicit authority (Phase U) |
| `29cdc77` | feat(interfaces): WhatsApp delivery intelligence (Phase W) |
| (this commit) | docs: ADRs + audit response + live probe |

---

# Addendum — Continuation Reconciliation (second principal-engineer pass)

A full reconciliation (repository ↔ Foundation PDF ↔ forensic audit ↔
ADRs ↔ tests ↔ live wiring) produced seven verified gaps. All are now
closed; each entry names its proof.

| # | Gap found | Category | Fix + proof |
|---|---|---|---|
| 1 | `CircuitBreaker` + `retry_with_backoff` implemented-but-unwired: LLM calls had no retry/breaker | reliability | `intelligence/resilience.py`; `tests/unit/test_provider_resilience.py` (blip-recovery, fail-fast, half-open, no-retry-on-401) |
| 2 | `http.get` had no network boundary: loopback / RFC1918 / metadata (169.254.169.254) / file:// reachable; unbounded body reads | security | `security/network.py` (address-truth validation, per-hop redirect re-validation, byte cap); `tests/unit/test_network_boundary.py` (14 tests) |
| 3 | Memory context = last-5-episodic-only (recency without relevance) | memory | `MemoryRepository.search_relevant` + ContinuityService relevance+recency composition with per-entry reasons; `tests/integration/test_memory_mechanisms.py::TestContextComposition` |
| 4 | Memory expiry never fired (`expire_due` had no caller) | cleanup | `memory/lifecycle.py` lifespan worker (soft delete + audit + metric); `TestLifecycleWorker` |
| 5 | AI had no gated memory agency (store/search/forget) | capability lifecycle | `memory.store` / `memory.search` / `memory.forget` capabilities through agency → budget → authority → invoker → audit; `TestMemoryCapabilities` (incl. cross-principal denial) |
| 6 | Model independence asserted but unproven (anthropic raised "not implemented") | model abstraction | `AnthropicProvider` (real /v1/messages wire format incl. tool_use/tool_result) + `ResilientProvider` on every selection; `TestAnthropicWire`, `TestFromSettings` |
| 7 | README was a 5-byte placeholder (audit: REFACTOR) | docs | Real README: OS-mental-model map, run/deploy, invariants, verification guide |

Commit series: `0e86946` (network boundary) → `490aa8c` (resilience +
anthropic) → `292be68` (memory cluster) → this commit (docs + probe
extension). Test count after this series: 408 (baseline at session
start: 373; original audit baseline: 312).

Open gaps intentionally NOT closed (evidence-based non-goals):
- Multi-replica work workers (lease design admits it; single-instance
  Railway deployment is the current honest scope — ADR-0004).
- Postgres tsvector/embedding retrieval upgrades (contract absorbs them;
  portable scorer is correct at current scale — ADR-0010).
- Human-approval workflow for agency-gated destructive actions
  (denial path is honest today; the workflow is a product decision
  requiring an approver identity story).

---

# Addendum 2 — Third Pass: Missing Primitives (environment capability)

Reconciliation #3 (`docs/engineering/reconciliation-3.md`) verified six
gaps. Five are closed below; the sixth (stale handoff) was rewritten. The
theme of this pass: **missing primitives**, not features — the environment
itself becoming capable of supporting open-ended work.

| # | Gap found | Category | Fix + proof |
|---|---|---|---|
| 1 | Durable waiting was a timer: work could wait only for a clock time (`available_at <= now`); no event/dependency/human-response conditions | waiting | ADR-0011: `runtime_signals` event ledger + `wake_kind` (time\|event) + watermark (no retroactive wakes) + `expires_at` (unmet conditions die honestly) + `signal.emit` (reserved namespaces runtime-owned: intelligence may wait on `interface.*`/`work.*`, never emit them) + runner announces terminal work states + bridge announces accepted messages. 10 tests (`TestEventWakeConditions`) + probe step 3c (live signal + live wake) |
| 2 | Memory revision/consolidation unreachable: `supersede()` had no caller; contradictions stayed duplicated; no evidence→durable path | memory | ADR-0012: `memory.store(supersedes=)` revision path (verify-before-create, ownership-checked) + `memory.consolidate` (N sources → one durable record, provenance=consolidation, supersession chain retained). 6 tests (`TestMemoryRevisionAndConsolidation`) + open-world scenario 2 |
| 3 | Context assembly discarded collected evidence: objective, conversation gap, summary, last execution status fetched but never delivered; no budget | context | ADR-0012: `wax.continuity.assembly` — labelled evidence sections (objective > conversation > memory), budget-aware fill by priority, announced truncation; system prompt stripped to persona + environment facts (principal id). 10 tests (`test_context_assembly.py`) |
| 4 | Scheduled work lost its trace: `work_schedule_impl` dropped the caller's execution context | continuity | work items now carry the scheduling execution's id (work → execution → objective traceability); test in `TestEventWakeConditions::test_scheduled_work_carries_execution_traceability` |
| 5 | Dead work was a graveyard: dead-letter had no re-drive path; a provider outage exhausting retries lost the objective forever | recovery | `work.requeue` capability: ownership-checked, dead-only, fresh provenance-linked item due now; dead item retained for audit. 3 tests (`TestWorkRequeue`) |
| 6 | `handoff.md` stale (claimed 208 tests / Phase R proposed) | docs | rewritten to current reality (this commit) |

**Open-world validation** (objectives nobody designed features for; every
one a pure composition, `tests/integration/test_open_world.py`):
1. "Continue when the user next messages you" — event wake on
   `interface.message:<principal>`; the reply wakes the work. Revealed and
   fixed an ordering bug: the interface signal is announced at message
   ACCEPTANCE, before intelligence runs, so a wait scheduled during message
   N waits for message N+1.
2. "Remember / correct / consolidate" — store → revise → consolidate
   across three messages; supersession chain verified end to end.
3. "Analyze data in a scratch workspace" — provision → authority-gated
   execute → remember; the acquisition loop (mission §17) with the
   security boundary intact.

**Deliberate non-goals (evidence-based, recorded in ADR-0011):**
package/dependency acquisition service; signal-ledger retention pruning;
model-advertised context limits; multi-replica workers; human-approval
workflow. Prior non-goals stand.

**Test count after this pass: 440** (session start: 408; original audit
baseline: 312). Live probe: 9/9 PASS over real HTTP (added: event-ledger
emission proof, live event-wake claim/run proof; message id now unique per
run so idempotency dedup cannot mask the live path).

---

# Addendum 3 — Fourth Pass: The Five Boundaries Became Mechanisms

The fourth pass started from `848da4d`. All prior claims were re-verified
independently (440 tests, 9/9 probe, 6 commits ahead, clean worktree). The
final report of the third pass had listed five "remaining boundaries" —
this pass re-evaluated each against the Universal Primitive Test and
implemented every one as infrastructure. Full working list:
`docs/engineering/reconciliation-4.md`.

| Boundary (as recorded) | Verdict | Mechanism now live | Proof |
|---|---|---|---|
| Package acquisition — design recorded, not built | infrastructure | `workspace.acquire`: mandatory sha256, host allowlist, SSRF-guarded egress, byte caps, content-addressed cache (hash-verified hits), atomic writes, owned-workspace isolation, provenance audit | 12 tests; ADR-0015 |
| Character-based context budget | infrastructure | provider-context negotiation (`wax.intelligence.context_limits`): advertised limit − reserve → budget; configured fallback; floor; adapter-local tokenizer knowledge | 12 tests; ADR-0015 |
| Single-instance worker | infrastructure | multi-worker correctness: SKIP LOCKED atomic claims, lease fencing (zombie writes refused), heartbeats (lease/3), loud reclaim-exhaustion deaths, graceful draining shutdown | 14 tests; ADR-0013 |
| No human-approval workflow | infrastructure | the generic approval primitive: pending approvals (idempotent fingerprint, expiry, provenance), human credential-path decisions (`/approve <id>`), one-time consumption, expiry sweep, AI can list/cancel — never decide | 16 tests + live probe; ADR-0014 |
| Signal ledger has no retention pruning | infrastructure | deterministic waiter-safe retention + bounded storage in `runtime.maintenance` | 10 tests + live probe; ADR-0015 |

Additional defects found and fixed during the pass: silent reclaim
exhaustion (now dead-letter + `work.dead` announcement), unannounced wait
expiry (`work.expired:<id>` now emitted), shutdown orphaning in-flight
work (bounded draining), model↔migration drift (`alembic check` now
clean), the destructive-scheduling guard superseded by the approval gate,
and the work handler's missing approval-consume path (found by open-world
scenario F and fixed).

Open-world validation expanded (wave 2, `tests/integration/test_open_world_2.py`):
capability-boundary honesty (not_found / unavailable / denied as distinct
structured outcomes), approval-gated durable work end to end, restart
continuity of scheduled work with effect visibility, provider substitution
with context-budget adaptation, and interface handoff (same principal, new
interface credential, memory evidence intact).

**Test count after this pass: 518** (session start 440; audit-era
baselines: 408 → 373 → 312). Live probe: **12 PASS checks** over real HTTP,
including the full approval flow (pending → webhook decision → exactly-once
execution) and a live ledger-retention pass.
