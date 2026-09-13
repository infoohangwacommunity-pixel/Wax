# WAX — Engineering Handoff

**Last updated:** 2026-09 (fourth principal-engineer pass)
**Branch:** `main`
**Total tests:** 518 passing
**Live probe:** 12 PASS checks (`python scripts/live_probe.py`)

## What WAX Is

WAX is an **open-world AI runtime**: a safe, durable, flexible environment
in which intelligence pursues legitimate human objectives. The model
provides intelligence; the runtime provides mechanisms; the human provides
the objective. There are no domain services (no ReminderService, no
StudyMode — permanently, INV-01). A reminder is a *composition*:
work.schedule (time wake) → capability handler → message.send.

## Architecture Map

```
                 HUMAN
                   │  (WhatsApp today — an interface adapter)
                   ▼
        ┌──────────────────────────┐
        │  RuntimeServices         │  construction point (lifespan)
        │──────────────────────────│
        │ Identity / Authority     │  interface-independent principals;
        │                          │  AI principal has zero permissions
        │ Security gate            │  rate limit → cost cap → abuse/
        │                          │  injection → trust-wrapped input
        │ Continuity               │  conversations + evidence assembly
        │                          │  (objective > conversation > memory,
        │                          │  budgeted, truncation announced)
        │ Intelligence             │  provider-agnostic (OpenAI/Anthropic/
        │                          │  Mock) + retry/breaker; tool-calling
        │                          │  contract over capability descriptors
        │ Capabilities             │  registry + invoker: EVERY effect goes
        │                          │  agency → budget → authority → audit
        │ Work runtime             │  durable work; wake by TIME or EVENT
        │                          │  (runtime_signals ledger); leases,
        │                          │  retries, dead-letter, requeue
        │ Provisioning             │  ephemeral resources (scratch dirs),
        │                          │  owner/TTL/limits/audit/sweeper
        │ Isolation                │  code.run: subprocess, env scrubbed,
        │                          │  network boundary on egress (ADR-0008)
        │ Memory                   │  store/revise/consolidate/search/
        │                          │  forget/expire — lifecycle enforced,
        │                          │  meaning owned by the model
        │ Observability            │  structured logs (redacting), metrics,
        │                          │  audit_events
        └──────────────────────────┘
```

## Core Mechanisms (all interface- and model-independent)

| Mechanism | Surface | Notes |
|---|---|---|
| Durable waiting | `work.schedule` with `wake_at`/`delay_seconds` **or** `wake_event` (+`not_before`, `expires_at`) | ADR-0011. Event waits correlate against the `runtime_signals` ledger; watermark makes waits never retroactive; broadcast semantics |
| Multi-worker runtime | atomic claims (SKIP LOCKED), lease fencing, heartbeats (lease/3), draining shutdown | ADR-0013. Zombie workers cannot write; reclaim-exhaustion and wait-expiry are ANNOUNCED (dead-letter + `work.dead`/`work.expired` signals) |
| Human approval | agency gate → pending approvals → `/approve <id>` / `/deny <id>` | ADR-0014. Idempotent fingerprint, expiry, one-time consumption, credential-path decisions; AI can list/cancel — never decide; durable work consumes approvals too |
| Runtime signals | `runtime_signals` table; `signal.emit` capability | `interface.*`, `work.*`, `approval.*` namespaces are runtime-owned (wait-only for the AI); waiter-safe retention in `runtime.maintenance` (ADR-0015) |
| Terminal-work announcement | automatic | `work.succeeded:<id>` / `work.dead:<id>` / `work.expired:<id>` enable dependency composition and honest reaction to death |
| Dead-work recovery | `work.requeue` | ownership-checked; provenance-linked fresh attempt; dead item kept for audit |
| Memory lifecycle | `memory.store` (+`supersedes`), `memory.consolidate`, `memory.search`, `memory.forget`, expiry worker | ADR-0012. Supersession = revision; consolidation = evidence → durable representation with provenance chain |
| Context assembly | `wax.continuity.assembly` + `wax.intelligence.context_limits` | objective/conversation/memory evidence, budget NEGOTIATED from the provider's advertised context limit (fallback configured), truncation announced |
| Artifact acquisition | `workspace.acquire` | ADR-0015. sha256 integrity mandatory, host allowlist, SSRF-guarded egress, content-addressed cache, owned-workspace isolation, provenance audit; acquisition never executes |
| Capability invocation | registry descriptors → tool specs; gated invoke loop | bounded rounds; honest structured failures (not_found/denied/timeout/pending_approval) |
| Execution environments | `scratch.workspace` + `code.run` | opt-in authority for code execution; egress through the network boundary |

## Invariants (enforced by tests)

| ID | Statement |
|---|---|
| INV-01 | Universal runtime mechanisms must not require any domain (education etc.) |
| INV-02 | Core WAX must not depend on any specific interface |
| INV-03 | Core WAX must not depend on any specific model provider |
| INV-04 | AI-requested actions must pass runtime authorization (AI has zero inherent permissions) |
| INV-05 | Important state survives process interruption |
| INV-06 | Security-sensitive actions are attributable (audit) |
| INV-07 | Major external dependencies have a replacement boundary |
| INV-08 | Unknown legitimate objectives require no ontology change |
| INV-09 | wax.core contains no I/O |
| INV-10 | No mock claimed as production infrastructure |

## How to Run / Verify

```bash
uv pip install -e ".[dev]"
pytest                                  # 518 tests
python scripts/live_probe.py            # 12 live-path checks over real HTTP
alembic upgrade head                    # migrations (SQLite for dev; Postgres for prod)
alembic check                           # must report zero drift
```

## Deployment (Railway)

1. Push to `main` — Railway auto-deploys via `railway.toml`.
2. Secrets: `WAX_SECRET_KEY`, `WAX_DATABASE_URL` (+ WhatsApp tokens when
   the interface is used).
3. Release runs `alembic upgrade head`, then `uvicorn wax.runtime.asgi:app`.

## Canonical Documents

- Constitution/principles: WAX Architectural Build Roadmap PDF + repo README
- Architecture: `docs/architecture/runtime-boundary.md`
- Decisions: `docs/decisions/ADR-0001..0015`
- Evidence: `docs/engineering/audit-response.md` (+ addenda),
  `docs/engineering/reconciliation-4.md`, worklog
- Probe: `scripts/live_probe.py` (the live-path gold standard)

## Current Boundaries (honest limits)

- Subprocess isolation for `code.run` is accident-isolation, not a
  sandbox; adversarial multi-tenant code needs container/microVM-grade
  isolation at the deployment layer (the isolation CONTRACT is in place).
- Tokenizer-exact accounting lives inside provider adapters when needed;
  the runtime core uses the conservative chars/token estimator (INV-03).
- The maintenance loop is idempotent and multi-instance-safe; no leader
  election (no correctness need at current scale).
- Postgres tsvector/embedding retrieval upgrades (ADR-0010) — the
  portable scorer remains correct at current scale.

Every former "recorded non-goal" from the third pass — package
acquisition, character-only budget, single-instance worker, human
approval, ledger pruning — is now a live mechanism (ADR-0013/0014/0015,
reconciliation-4).

## What NOT to Change Casually

- `wax.core.invariants` — constitutional; additions fine, weakening needs an ADR.
- `wax.authority.seed` — AI principal has no inherent permissions.
- `wax.security.network` — capability egress boundary (ADR-0008).
- `wax.isolation.subprocess_boundary._prepare_command` env filter — secret boundary.
- `wax.runtime.work.signals.validate_signal_name` — namespace trust boundary.
- `wax.runtime.logging._redact_sensitive` — keep the sensitive-key list conservative.
