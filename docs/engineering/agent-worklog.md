# WAX Implementation Agent Worklog (Open-World Runtime Continuation)

Mission: continue WAX as an open-world AI runtime. Fix verified live-path breaks,
wire implemented-but-unwired subsystems, then build universal runtime mechanisms
(durable work, provisioning, capability discovery, execution environments,
background runtime, interface intelligence). No hardcoded use cases.

Baseline: commit 5b0894e (main), 312 tests passing — verified locally before any change.
Audit source: WAX_Current_Repository_Reality_Report.pdf (41-page forensic audit).

---

## Task 2 — Wire the implemented-but-unwired runtime (commit 2)

- New `runtime/services.py`: RuntimeServices container — the construction
  point the audit's "TODO Phase G" comment marked as the edge of the wired
  system. Builds capability registry (+ built-ins), rate limiter, cost
  protector, abuse detector, input sanitizer, resource accountant, metrics
  facade, delivery router. Per-session authority/agency/invoker factories.
- New `authority/seed.py`: idempotent BUILTIN_ROLES seeding + default
  "member" role assignment on first contact (audit §7: principals had zero
  permissions forever).
- New `observability/runtime_metrics.py` + `observability/audit.py`: the
  live path now emits metrics and writes audit_events (audit §17/§18/§20:
  registry was always empty; zero audit rows after real traffic).
- Bridge pipeline rework (runtime/bridge/service.py):
  * security gate BEFORE intelligence: rate limit → daily cost cap →
    abuse/injection verdicts; rejections are dedup-locked + audited.
  * honest failure semantics: execution+objective marked failed; idempotency
    record retryable ("failed") with attempts counter; terminal after
    MAX_ATTEMPTS_PER_MESSAGE → "dead" + dead_letter_entries row (audit
    Scenario 5: failures were terminal, silent, and unrecoverable).
  * "work accepted" checkpoint committed BEFORE the LLM call, so identity +
    dedup lock + objective + execution survive crashes.
  * objective lifecycle: pending → in_progress → succeeded/failed (audit
    §15: objectives stayed pending forever).
  * continuity: ConversationService open/resume/touch + ContinuityService
    as the single context composer (audit §28 duplication resolved).
  * credential last_used_at now updated on return visits (audit §7).
  * resource budget allocation per execution; LLM calls + tokens consumed
    through the ResourceAccountant; token usage feeds the CostProtector.
- Lifespan (runtime/app.py): builds RuntimeServices, seeds roles, registers
  the WhatsApp sender with the DeliveryRouter, passes services to bridge.
- Send-path repair (audit §28): the runtime callback now RETURNS the
  response text; the adapter performs the send (its previously-dead branch
  is live). Added on_send_failure hook → dead-letter for lost replies.
- Metrics snapshot is JSON-safe (+Inf bounds → "+Inf").
- Config: llm_base_url, llm_model, max_tool_rounds, work_poll_interval_seconds,
  provisioning_root, provisioning_max_active_per_principal.
- Tests: +12 wiring-proof tests (test_runtime_wiring.py) each pinning a
  previously-false audit claim. 322 → 334 passing.
