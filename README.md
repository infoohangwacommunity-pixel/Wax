# WAX — An Open-World AI Runtime

WAX is not a chatbot and not a collection of features. It is a **runtime
environment in which an AI intelligence pursues human objectives** — the
runtime provides mechanisms (durable work, capability enforcement,
authority, memory, provisioning, delivery, observability); the AI provides
the intelligence. Education (today via WhatsApp) is its first mission, not
its boundary.

> "The system should provide an environment and mechanisms; the
> intelligence should determine how those mechanisms are composed to
> pursue legitimate objectives." — Foundation Notes, §3

## The mental model

WAX is built like an operating system, not like an app:

| An OS provides… | WAX provides… |
|---|---|
| processes & scheduling | durable work + wake conditions (`work_items`: time or event wake, leases, retries, dead-letter, requeue) |
| wait/notify & signals | the `runtime_signals` event ledger (`interface.*`/`work.*` runtime-owned; gated AI emission) |
| permissions | Authority & Agency (the AI has agency; the runtime has sovereignty) |
| syscalls | Capabilities (the sole effect path — always gated, audited, metered) |
| filesystem | dynamic provisioning (ephemeral resources with owner/TTL/limits/cleanup) |
| memory | memory mechanisms (evidence, revision, consolidation, relevance retrieval, enforced forgetting) |
| network stack | interface abstraction (WhatsApp first, never the architecture) + egress boundary |
| device drivers | model adapters (mock / OpenAI-compatible / Anthropic behind one contract) |

Applications emerge from composition. "Remind me in one hour" is not a
feature — it is `work.schedule` → wake → `message.send`, composed by the
AI through gated capabilities. Neither is "ping me when I reply" — that
is `work.schedule(wake_event="interface.message:<principal>")`.

## Architecture map

```
src/wax/
  core/           config, exceptions, invariants, logging contracts
  state/          persistence (SQLAlchemy models, migrations, engine)
  identity/       interface-independent principals
  authority/      roles, permissions, enforcement (AI principal: no inherent rights)
  agency/         policy decisions incl. destructive-action gates
  capabilities/   registry + invoker (sole effect enforcement point, INV-04)
                  built-ins: echo, http.get (network-boundaried), code.run
                  runtime:  work.schedule/cancel/list/requeue, signal.emit,
                            message.send, scratch.workspace,
                            memory.store/search/forget/consolidate
  execution/      durable executions with checkpoints + steps
  runtime/        app wiring, RuntimeServices container, RuntimeBridge,
                  durable-work runner, provisioning service
  intelligence/   LLM contracts + adapters (mock, OpenAI-compatible,
                  Anthropic) + ResilientProvider (retry + circuit breaker)
  memory/         evidence storage, relevance retrieval, lifecycle worker
  continuity/     conversation lifecycle + context assembly
  security/       rate limiting, abuse, input sanitization, cost caps,
                  network egress boundary (SSRF)
  reliability/    retries, circuit breakers, dead letters
  resources/      per-execution budgets and accounting
  observability/  metrics, structured logging, append-only audit events
  interfaces/     WhatsApp Cloud API adapter (interface owns wire limits)
  isolation/      subprocess boundary (code.run execution)
  media/          extraction pipeline (runtime extracts, AI interprets)
```

Documentation that matters:

- `docs/architecture/runtime-boundary.md` — the layer model + invariants
- `docs/decisions/ADR-0001…0012` — every consequential decision and why
- `docs/engineering/audit-response.md` — forensic-audit disposition with proofs
- `docs/engineering/handoff.md`, `docs/engineering/worklog.md` — engineering history

## Running it

```bash
python -m venv .venv && source .venv/bin/activate
uv pip install -e ".[dev]"        # or pip install -e ".[dev]"
alembic upgrade head              # apply migrations
pytest                            # full test suite
python scripts/live_probe.py      # boots the real app; exercises the
                                  # webhook + live path over real HTTP
```

Configure via environment (all prefixed `WAX_`): `WAX_DATABASE_URL`,
`WAX_WHATSAPP_*` (access token, phone number id, app secret, verify
token), `WAX_LLM_DEFAULT_PROVIDER` (`mock` | `openai` | `anthropic`),
`WAX_OPENAI_API_KEY` / `WAX_ANTHROPIC_API_KEY`, `WAX_LLM_MODEL`,
`WAX_LLM_BASE_URL` (any OpenAI-compatible endpoint works). See
`docs/operations/production-deployment.md` for deployment.

## Constitutional invariants (enforced by tests)

- **INV-01** No domain concepts in `wax.core` — education is the mission,
  never the ontology.
- **INV-03** No provider SDK outside its adapter file.
- **INV-04** The AI principal has no inherent permissions; every effect
  crosses Authority via the CapabilityInvoker.
- **INV-06/09** No I/O in core; the boundary is tested, not aspirational.
- Never fake success: failures surface as real failures (retries,
  dead-letters, truthful constraint messages).

## Verifying claims

Every architectural claim in this repository is backed by a named test,
a live probe (`scripts/live_probe.py`), or an ADR with its evidence. The
audit-response document maps each audit finding to its disposition and
proof. Start there if you want to trust nothing and verify everything —
that stance is the intended one.
