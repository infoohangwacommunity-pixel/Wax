# WAX — Constitutional Audit (Article 25)

_Generated: 2026-09-14T18:06:06.277757+00:00_

This audit scans the repository for violations of the six constitutional laws (Article 2). Each finding is a candidate violation — every finding requires human review per Article 25's 'Never silently delete' rule.

## LAW 1 — Infrastructure Never Thinks

**Total findings: 0**

No candidate violations detected by this scanner.

## LAW 2 — Open World (brand leakage outside adapters)

**Total findings: 65**

<details><summary>Findings (click to expand)</summary>

| File | Line | Detail | Context |
|---|---|---|---|
| src/wax/authority/seed.py | 1 | brand=whatsapp | `Role seeding and default role assignment.

The forensic audit (Section 7) found that principals created via WhatsApp
wer` |
| src/wax/core/invariants.py | 56 | brand=whatsapp | `Core WAX must not depend on any specific interface (WhatsApp, web, etc.).` |
| src/wax/core/invariants.py | 58 | brand=whatsapp | `Interfaces are adapters into WAX, not the definition of WAX. If WhatsApp disappears, WAX must still exist as a runtime. ` |
| src/wax/core/config.py | 173 | brand=github | `files.pythonhosted.org,github.com,objects.githubusercontent.com,raw.githubusercontent.com,registry.npmjs.org` |
| src/wax/state/identity_models.py | 27 | brand=whatsapp | `A universal WAX principal — a human or service that can be authorized.

    A principal is intentionally minimal: it has` |
| src/wax/intelligence/service.py | 1 | brand=openai | `IntelligenceService — the runtime's LLM routing layer.

The runtime owns the provider. The AI requests intelligence via ` |
| src/wax/intelligence/service.py | 1 | brand=anthropic | `IntelligenceService — the runtime's LLM routing layer.

The runtime owns the provider. The AI requests intelligence via ` |
| src/wax/intelligence/service.py | 69 | brand=openai | `Build the service from WaxSettings.

        Provider selection:
        - If WAX_LLM_DEFAULT_PROVIDER is "mock" or empt` |
| src/wax/intelligence/service.py | 69 | brand=anthropic | `Build the service from WaxSettings.

        Provider selection:
        - If WAX_LLM_DEFAULT_PROVIDER is "mock" or empt` |
| src/wax/intelligence/service.py | 133 | brand=openai | `. Supported: mock, openai, anthropic` |
| src/wax/intelligence/service.py | 133 | brand=anthropic | `. Supported: mock, openai, anthropic` |
| src/wax/intelligence/service.py | 226 | brand=openai | `. Supported: mock, openai, anthropic` |
| src/wax/intelligence/service.py | 226 | brand=anthropic | `. Supported: mock, openai, anthropic` |
| src/wax/intelligence/service.py | 92 | brand=openai | `WAX_LLM_DEFAULT_PROVIDER=openai requires WAX_OPENAI_API_KEY` |
| src/wax/intelligence/service.py | 114 | brand=anthropic | `WAX_LLM_DEFAULT_PROVIDER=anthropic requires WAX_ANTHROPIC_API_KEY` |
| src/wax/intelligence/service.py | 199 | brand=openai | `provider fallback 'openai' requires WAX_OPENAI_API_KEY` |
| src/wax/intelligence/service.py | 211 | brand=anthropic | `provider fallback 'anthropic' requires WAX_ANTHROPIC_API_KEY` |
| src/wax/intelligence/service.py | 205 | brand=openai | `https://api.openai.com/v1` |
| src/wax/intelligence/service.py | 222 | brand=anthropic | `https://api.anthropic.com/v1` |
| src/wax/intelligence/service.py | 103 | brand=openai | `https://api.openai.com/v1` |
| src/wax/intelligence/service.py | 123 | brand=anthropic | `https://api.anthropic.com/v1` |
| src/wax/intelligence/__init__.py | 1 | brand=openai | `wax.intelligence — LLM provider abstraction.

The model must be replaceable. A specific provider, model family, or vendo` |
| src/wax/intelligence/__init__.py | 1 | brand=anthropic | `wax.intelligence — LLM provider abstraction.

The model must be replaceable. A specific provider, model family, or vendo` |
| src/wax/intelligence/context_limits.py | 1 | brand=openai | `Context-budget negotiation — how much evidence fits this model?

The runtime assembles evidence under a BUDGET (ADR-0012` |
| src/wax/intelligence/contracts.py | 23 | brand=openai | `(identifier)` |
| src/wax/intelligence/contracts.py | 24 | brand=anthropic | `(identifier)` |
| src/wax/identity/__init__.py | 1 | brand=whatsapp | `wax.identity — interface-independent identity for WAX.

The identity system is the foundation of authorization. WAX iden` |
| src/wax/identity/repository.py | 148 | brand=whatsapp | `Find the principal associated with a (kind, value) credential.

        This is the core of interface-independent identi` |
| src/wax/identity/contracts.py | 22 | brand=whatsapp | `whatsapp` |
| src/wax/identity/contracts.py | 24 | brand=telegram | `telegram` |
| src/wax/runtime/__init__.py | 1 | brand=whatsapp | `wax.runtime — the WAX runtime process.

This package contains the running WAX process: the FastAPI application,
lifecycl` |
| src/wax/runtime/delivery.py | 1 | brand=whatsapp | `Delivery router — how the runtime sends outbound messages, interface-agnostically.

Phase W (Interface Intelligence) req` |
| src/wax/runtime/delivery.py | 1 | brand=telegram | `Delivery router — how the runtime sends outbound messages, interface-agnostically.

Phase W (Interface Intelligence) req` |
| src/wax/runtime/services.py | 1 | brand=whatsapp | `RuntimeServices — the process-wide service container.

The forensic audit (Section 33) located the boundary of the wired` |
| src/wax/runtime/app.py | 1 | brand=whatsapp | `WAX FastAPI application factory.

This module builds the FastAPI app, wires up middleware, routes, lifecycle,
and the he` |
| src/wax/runtime/app.py | 1 | brand=telegram | `WAX FastAPI application factory.

This module builds the FastAPI app, wires up middleware, routes, lifecycle,
and the he` |
| src/wax/runtime/app.py | 259 | brand=whatsapp | `(identifier)` |
| src/wax/runtime/app.py | 381 | brand=whatsapp | `Meta's webhook verification endpoint.

        Meta sends: GET /webhooks/whatsapp?hub.mode=subscribe
        &hub.verify` |
| src/wax/runtime/app.py | 375 | brand=whatsapp | `/webhooks/whatsapp` |
| src/wax/runtime/app.py | 409 | brand=whatsapp | `Receive WhatsApp webhook events.

        Binding notes (both defects were verified live by the forensic
        audit a` |
| src/wax/runtime/app.py | 404 | brand=whatsapp | `/webhooks/whatsapp` |
| src/wax/runtime/app.py | 433 | brand=whatsapp | `Convert the WhatsApp message → RuntimeRequest → process via bridge.

            This is the SOLE place where WhatsApp s` |
| src/wax/runtime/app.py | 113 | brand=whatsapp | `whatsapp.close` |
| src/wax/runtime/app.py | 121 | brand=whatsapp | `whatsapp` |
| src/wax/runtime/app.py | 134 | brand=whatsapp | `whatsapp.client.initialized` |
| src/wax/runtime/app.py | 138 | brand=whatsapp | `whatsapp.client.not_configured` |
| src/wax/runtime/app.py | 261 | brand=whatsapp | `whatsapp` |
| src/wax/runtime/app.py | 263 | brand=whatsapp | `whatsapp` |
| src/wax/runtime/app.py | 451 | brand=whatsapp | `whatsapp.bridge_not_configured` |
| src/wax/runtime/app.py | 483 | brand=whatsapp | `whatsapp.duplicate_message.skipped` |
| src/wax/runtime/app.py | 490 | brand=whatsapp | `whatsapp.message.not_replied` |
| src/wax/runtime/app.py | 514 | brand=whatsapp | `whatsapp` |
| src/wax/runtime/app.py | 569 | brand=whatsapp | `whatsapp.delivery_record_write_failed` |
| src/wax/runtime/app.py | 530 | brand=whatsapp | `whatsapp.send_failure.unresolvable_principal` |
| src/wax/runtime/app.py | 566 | brand=whatsapp | `whatsapp` |
| src/wax/runtime/app.py | 546 | brand=whatsapp | `whatsapp` |
| src/wax/media/__init__.py | 1 | brand=whatsapp | `wax.media — Media Intelligence Pipeline.

When a user sends an image, audio, document, or video via WhatsApp, the
runtim` |
| src/wax/runtime/bridge/__init__.py | 1 | brand=whatsapp | `wax.runtime.bridge — canonical bridge between interfaces and runtime.

This is the universal entrypoint that ANY interfa` |
| src/wax/runtime/bridge/__init__.py | 1 | brand=telegram | `wax.runtime.bridge — canonical bridge between interfaces and runtime.

This is the universal entrypoint that ANY interfa` |
| src/wax/runtime/bridge/contracts.py | 1 | brand=whatsapp | `Canonical contracts for the runtime bridge.

These are interface-agnostic. A WhatsApp message, a web chat message,
and a` |
| src/wax/runtime/bridge/contracts.py | 1 | brand=telegram | `Canonical contracts for the runtime bridge.

These are interface-agnostic. A WhatsApp message, a web chat message,
and a` |
| src/wax/runtime/bridge/contracts.py | 27 | brand=whatsapp | `(identifier)` |
| src/wax/runtime/bridge/contracts.py | 29 | brand=telegram | `(identifier)` |
| src/wax/runtime/bridge/contracts.py | 52 | brand=whatsapp | `A canonical runtime request, interface-agnostic.

    The interface adapter builds this from its platform-specific shape` |
| src/wax/runtime/bridge/contracts.py | 104 | brand=whatsapp | `A canonical runtime response, interface-agnostic.

    The interface adapter translates this into its platform-specific ` |

</details>

## LAW 3 — Runtime Authority (model authority claims)

**Total findings: 0**

No candidate violations detected by this scanner.

## LAW 4 — Secrets Never Enter Intelligence

**Total findings: 6**

<details><summary>Findings (click to expand)</summary>

| File | Line | Detail | Context |
|---|---|---|---|
| src/wax/intelligence/service.py | 73 | pattern=\bWAX_[A-Z_]+_API_KEY\b | `- If "openai" → OpenAIProvider (requires [REDACTED])` |
| src/wax/intelligence/service.py | 74 | pattern=\bWAX_[A-Z_]+_API_KEY\b | `- If "anthropic" → AnthropicProvider (requires [REDACTED])` |
| src/wax/intelligence/service.py | 92 | pattern=\bWAX_[A-Z_]+_API_KEY\b | `"WAX_LLM_DEFAULT_PROVIDER=openai requires [REDACTED]"` |
| src/wax/intelligence/service.py | 114 | pattern=\bWAX_[A-Z_]+_API_KEY\b | `"WAX_LLM_DEFAULT_PROVIDER=anthropic requires [REDACTED]"` |
| src/wax/intelligence/service.py | 199 | pattern=\bWAX_[A-Z_]+_API_KEY\b | `"provider fallback 'openai' requires [REDACTED]"` |
| src/wax/intelligence/service.py | 211 | pattern=\bWAX_[A-Z_]+_API_KEY\b | `"provider fallback 'anthropic' requires [REDACTED]"` |

</details>

## LAW 5 — Evidence Before Claims (returns success without evidence)

**Total findings: 0**

No candidate violations detected by this scanner.

## LAW 6 — Every Component Must Justify Its Existence

**Unwired module files:** 3

<details><summary>Unwired modules</summary>

- `src/wax/core/invariants.py  (parent imported; specific module not directly imported)`
- `src/wax/runtime/asgi.py  (parent imported; specific module not directly imported)`
- `src/wax/security/trust.py  (parent imported; specific module not directly imported)`

</details>

**Files without module docstring:** 0

## Article 4 — Component Classification

Each subsystem is classified into exactly one of: LIVE, IMPLEMENTED BUT UNWIRED, TEST ONLY, PLACEHOLDER, DEAD, PHILOSOPHY VIOLATION, MISSING PRIMITIVE.

| Subsystem | Classification | Justification |
|---|---|---|
| `agency` | **LIVE** | policy decisions + destructive-action gate |
| `authority` | **LIVE** | roles/permissions/gate/approvals — verified-credential filter in place |
| `capabilities` | **LIVE** | registry + invoker + idempotency ledger — proven by live probe |
| `continuity` | **LIVE** | conversation lifecycle + context assembly |
| `core` | **LIVE** | config/invariants/exceptions — imported everywhere; no findings |
| `execution` | **LIVE** | checkpoints/steps — but production recovery is incomplete (Cycle 3 gap) |
| `identity` | **LIVE** | principal/credential models + repository |
| `intelligence` | **LIVE** | mock/openai/anthropic adapters + ResilientProvider + context budget |
| `intelligence/adapters/anthropic_provider` | **LIVE** | Anthropic SDK isolated to adapter file |
| `intelligence/adapters/mock_provider` | **LIVE** | Test/CI provider |
| `intelligence/adapters/openai_provider` | **LIVE** | OpenAI-compatible adapter with tiktoken-exact counter |
| `interfaces/whatsapp` | **LIVE** | WhatsApp Cloud API adapter — proven by live probe |
| `isolation` | **LIVE** | namespace sandbox + subprocess boundary |
| `media` | **IMPLEMENTED BUT UNWIRED** | Subsystem is an orphan — no other subsystem imports it. Real extractors exist; inbound path needs Cycle 8 work. |
| `memory` | **LIVE** | evidence + lifecycle + retrieval; contradiction preservation present |
| `objective` | **LIVE** | lifecycle + execution history + evidence-based completion |
| `observability` | **LIVE** | metrics + structured logging + append-only audit |
| `reliability` | **LIVE** | retries + circuit breakers + dead letters |
| `resources` | **LIVE** | per-execution budgets + accounting |
| `runtime/app` | **LIVE** | create_app composition root |
| `runtime/asgi` | **LIVE** | ASGI entry / lifespan wiring |
| `runtime/bridge` | **LIVE** | RuntimeBridge wiring — proven by live probe end-to-end |
| `runtime/delivery` | **LIVE** | delivery records + retry queue (ADR-0021) |
| `runtime/delivery_queue` | **IMPLEMENTED BUT UNWIRED** | Queue record exists; full multi-instance leadership is Cycle 10 |
| `runtime/leadership` | **LIVE** | per-pass maintenance leader |
| `runtime/maintenance` | **LIVE** | advisory-lock leader election + retention pass |
| `runtime/provisioning` | **LIVE** | dynamic provisioning + leases |
| `runtime/services` | **LIVE** | RuntimeServices container |
| `runtime/work` | **LIVE** | durable work + signals — proven by live probe event-wake |
| `security` | **LIVE** | rate limiting + abuse + input sanitizer + cost caps + SSRF boundary |
| `state` | **LIVE** | 17 models + engine; backed by 12 migrations; alembic check drift exists (size_bytes) |

## Article 26 — Missing Universal Primitives (Cycle Frontier)

Each item is the constitutional gap that the corresponding cycle (per ARTICLE 27) must close.

| Primitive | Cycle | Status |
|---|---|---|
| Durable intelligence re-entry | Cycle 3 (frontier) | Partial local work — not in this clone (no ADR-0034 in repo; ADRs stop at 0028). |
| Checkpoint recovery | Cycle 4 | Repository checkpoint methods exist; production resumption incomplete. |
| Environment negotiation protocol | Cycle 5 | Provisioning exists; requirement/planning/negotiation incomplete. |
| Generic connector credential vault | Cycle 6 | Interface credentials exist; scoped grant/injection/rotation incomplete. |
| Workspace + terminal lifecycle | Cycle 7 | Workspace_acquire + artifact records exist; full publish/export/delivery incomplete. |
| Safe open capability acquisition | Cycle 8 | Registry closed at boot (honest). Acquisition lifecycle deferred. |
| Media → evidence → context pipeline | Cycle 9 | Extractors exist; adapter-to-runtime path incomplete. `media` subsystem is also orphaned. |
| Approval expiry + objective reconciliation | Cycle 10 | Approval primitive + idempotency exist; expiry re-drive incomplete. |
| Multi-instance enforcement | Cycle 11 | Single-writer advisory lock present; multi-process claims not yet testable. |
| Retention governance | Cycle 12 | Founder decisions required before policy can be hardcoded. |
| Evaluation harness | Cycle 13 | tests/evaluation/ exists with 1 file; full harness incomplete. |

## Honest limitations of this audit

- This is a static AST/regex scan; it does not execute the code paths. Some findings may be false positives (legitimate string constants that mention a brand for documentation, etc.).
- The audit does NOT include the 'PHILOSOPHY VIOLATION' classification automatically — that requires human judgment and is captured in the table above only where obvious.
- Audit does not detect prompt-injection risks (those require runtime trace analysis, not static scan).
- Audit does not verify the security boundaries actually fire under load — that requires the security test suite, which the test suite itself covers (see Test Report).
