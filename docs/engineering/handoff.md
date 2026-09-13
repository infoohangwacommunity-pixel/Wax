# WAX — Engineering Handoff

**Last updated:** 2026-09 (third principal-engineer pass)
**Branch:** `main`
**Total tests:** 440 passing
**Live probe:** 9/9 PASS (`python scripts/live_probe.py`)

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
| Runtime signals | `runtime_signals` table; `signal.emit` capability | `interface.*` and `work.*` namespaces are runtime-owned (wait-only for the AI) |
| Terminal-work announcement | automatic | `work.succeeded:<id>` / `work.dead:<id>` enable dependency composition ("run B when A finishes") |
| Dead-work recovery | `work.requeue` | ownership-checked; provenance-linked fresh attempt; dead item kept for audit |
| Memory lifecycle | `memory.store` (+`supersedes`), `memory.consolidate`, `memory.search`, `memory.forget`, expiry worker | ADR-0012. Supersession = revision; consolidation = evidence → durable representation with provenance chain |
| Context assembly | `wax.continuity.assembly` | objective/conversation/memory evidence, budgeted by priority, truncation announced |
| Capability invocation | registry descriptors → tool specs; gated invoke loop | bounded rounds; honest structured failures (not_found/denied/timeout) |
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
pytest                                  # 440 tests
python scripts/live_probe.py            # 9/9 live-path checks over real HTTP
alembic upgrade head                    # migrations (SQLite for dev; Postgres for prod)
```

## Deployment (Railway)

1. Push to `main` — Railway auto-deploys via `railway.toml`.
2. Secrets: `WAX_SECRET_KEY`, `WAX_DATABASE_URL` (+ WhatsApp tokens when
   the interface is used).
3. Release runs `alembic upgrade head`, then `uvicorn wax.runtime.asgi:app`.

## Canonical Documents

- Constitution/principles: WAX Architectural Build Roadmap PDF + repo README
- Architecture: `docs/architecture/runtime-boundary.md`
- Decisions: `docs/decisions/ADR-0001..0012`
- Evidence: `docs/engineering/audit-response.md` (+ addenda),
  `docs/engineering/reconciliation-3.md`, worklog
- Probe: `scripts/live_probe.py` (the live-path gold standard)

## Current Boundaries (honest limits)

- Single-instance worker (lease design admits replicas; not needed at
  current scale — ADR-0004/0011).
- No human-approval workflow yet: destructive actions are denied honestly
  (the denial path is real; the approval loop is a product decision).
- Character-based context budget (~4 chars/token), not tokenizer-exact.
- No package/dependency acquisition service (design constraints recorded
  in ADR-0011 §Future).
- Signal ledger has no retention pruning yet (rows are small; pruning is
  a recorded follow-up).

## What NOT to Change Casually

- `wax.core.invariants` — constitutional; additions fine, weakening needs an ADR.
- `wax.authority.seed` — AI principal has no inherent permissions.
- `wax.security.network` — capability egress boundary (ADR-0008).
- `wax.isolation.subprocess_boundary._prepare_command` env filter — secret boundary.
- `wax.runtime.work.signals.validate_signal_name` — namespace trust boundary.
- `wax.runtime.logging._redact_sensitive` — keep the sensitive-key list conservative.
