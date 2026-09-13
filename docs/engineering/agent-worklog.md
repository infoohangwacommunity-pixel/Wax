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

## Task 3 — Tool-calling: Intelligence → Capability invocation (commit 3)

- LLM contract: ToolSpec + ToolCall dataclasses; LLMRequest.tools;
  LLMResponse.tool_calls; LLMMessage.tool_calls/tool_call_id for verbatim
  conversation replay. Backward compatible.
- OpenAI adapter: emits tools[] + tool_choice=auto; parses tool_calls
  (JSON-argument tolerant); translates assistant-tool-turns and tool
  results to the OpenAI wire format.
- Mock provider: explicit scripted_tool_calls test seam (deque of batches)
  - documented as test infrastructure, never production behavior.
- Bridge: bounded agentic loop (max_tool_rounds) replacing the single LLM
  call. Each tool request passes: agency gate (INVOKE_CAPABILITY→reversible
  auto-approve; is_destructive→DESTRUCTIVE_ACTION→human approval required →
  denied honestly, since no approval workflow exists yet) → capability
  budget consumption → CapabilityInvoker (authority + audit) → structured
  result back to the model as a tool message. Every attempt records an
  execution step (capability.invoke) and a metric.
- IntelligenceService.from_settings now passes llm_base_url + llm_model
  from configuration (audit §14: base_url was unreachable via config).
- 334 → 340 tests (test_tool_calling.py: full-gate echo invocation, honest
  not_found, destructive-denial, loop bound, OpenAI wire format).

## Task 4 — Phase R/V: Durable work runtime (commit 4)

- New table `work_items` (migration b7f21c9d4e02, round-trip verified on
  SQLite): kind (runtime mechanism name), opaque payload, wake_at,
  available_at, attempts/max_attempts, lease_owner/lease_expires_at,
  status pending→leased→running→succeeded|failed(retry)|dead|cancelled.
- WorkRepository: schedule / claim_due (crash-safe leases; expired leases
  reclaimable; reclaim consumes an attempt; exhaustion → dead) /
  mark_succeeded / mark_failed (exponential backoff slot) / cancel /
  list_for_principal.
- WorkRunner: in-process asyncio worker started in the lifespan (no second
  process needed; lease design admits replicas later). Bounded retries,
  dead-letter rows on exhaustion, in-flight gauge metric.
- Recovery scan at startup: executions stuck "running" by a dead process →
  failed; their processed_messages pending → failed (retryable on Meta
  redelivery). Closes the audit §8 crash hole.
- capability_handler: the universal "wake and invoke" handler — same gate
  chain as the live path (agency → budget → authority → invoker).
- Runtime capabilities (closures over the container, no globals):
  work.schedule (honest early refusal of unknown/destructive targets),
  work.cancel (ownership enforced), work.list, message.send (recipient
  must be the caller's own verified credential; Meta 24-hour window
  enforced truthfully — no invented templates).
- CapabilityImpl protocol now passes InvocationContext(principal_id,
  capability_name, execution_id, request_id); built-ins + invoker updated.
- THE COMPOSITION TEST: user message → scripted AI calls work.schedule →
  runner wakes → message.send delivers. A reminder exists as a
  MECHANISM COMPOSITION; there is no ReminderService/TimerService.
- 340 → 352 tests.

## Task 5 — Phase S: Dynamic provisioning (commit 5)

- New table `provisioned_resources` (migration c3a95f1e8b21, chain
  verified): kind / status / principal_id / execution_id / uri / expires_at
  / limits / metadata.
- ProvisioningService: provision_scratch_dir (per-principal active limit,
  TTL cap 24h, path under provisioning_root, audit event),
  release (owner-only), expire_due (TTL reaper; audit row survives),
  promote (the ONLY path to permanence — clears TTL with audit).
- Path containment: destroy() refuses paths outside provisioning_root, so
  a tampered row can never point the reaper at arbitrary paths.
- scratch.workspace capability: the AI can request an ephemeral scratch
  directory; honest refusal on invalid TTL / limit exhaustion.
- Lifespan: maintenance reaper task (60s interval) with clean shutdown.
- 352 -> 360 tests (test_provisioning.py).
