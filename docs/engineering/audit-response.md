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
