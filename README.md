# WAX

WAX is an AI runtime. It provides intelligence with:

- **memory** — continuity across time
- **a terminal environment** — the universal interface to the world
- **persistent execution** — what happened, what's happening
- **durable reentry** — what should continue later
- **identity** — whose world it is
- **communication** — how the result reaches the user

The intelligence determines how to accomplish work. The runtime provides
the mechanisms.

## Philosophy

WAX is an **open-world intelligence runtime**. The runtime does not
maintain a closed model-facing capability catalogue. The runtime does
not use an authority/approval broker. The runtime does not constrain
the intelligence through a permission system.

Instead, the intelligence operates a **terminal** — a general execution
environment with full network access, full filesystem access, and
inherited environment variables. The terminal is the environment
interface. The intelligence composes actions using whatever the
environment provides (git, curl, python, node, apt, etc.).

This is a **full-trust architecture**: the intelligence is trusted to
operate the environment. Operational process limits (timeout, output
truncation) are execution plumbing, not action authorization.

## Architecture

```
User → Interface (WhatsApp) → Identity + Idempotency → Execution
     → Memory Retrieval → Context Assembly
     → Intelligence ↔ Terminal (loop)
     → Memory Extraction → Execution History
     → Delivery → Durable Work (when necessary) → Reentry
```

The loop: **remember → understand → act → observe → learn → continue**.

## What's here

```
src/wax/
  core/           config, exceptions, invariants
  identity/       principals + credentials
  intelligence/   LLM providers (OpenAI, Anthropic, mock) + resilience
  interfaces/     WhatsApp adapter
  memory/         memory records + retrieval + lifecycle
  execution/      execution history + steps + recovery
  continuity/     conversation threading + context assembly
  runtime/
    app.py         FastAPI factory
    asgi.py        ASGI entrypoint
    services.py    process-wide service container
    executor.py    the terminal executor (core of the open-world architecture)
    bridge/        the intelligence ↔ terminal loop
    work/          durable work + reentry
    delivery.py    outbound delivery router
    delivery_queue.py  retry-able delivery records
    maintenance.py lifecycle hygiene (signals, delivery, conversations, audit)
    lifecycle.py   signal handlers + shutdown
    logging.py     structured logging
  state/          SQLAlchemy models + engine
  observability/  audit events (structured logs are in runtime.logging)
  reliability/    retries + circuit breakers
```

## What's NOT here (intentionally removed)

- No `capabilities/` — the capability registry is gone
- No `authority/` — no roles, permissions, approvals, or gates
- No `resources/` — no resource accountant or budgets
- No `isolation/` — no sandbox, namespace, or subprocess boundary
- No `objective/` — no objective state machine
- No `security/` — no network allowlist, path containment, or SSRF guard
- No `runtime/control_plane.py` — no dashboard
- No `runtime/leadership.py` — no multi-instance leader election
- No `runtime/provisioning.py` — no provisioning service
- No `runtime/blob_store.py` — no workspace snapshots
- No `observability/metrics.py` — no Prometheus-style metrics subsystem

## Running

```bash
# Install
pip install -e ".[dev]"

# Run migrations
alembic upgrade head

# Start the runtime
uvicorn wax.runtime.asgi:app --host 0.0.0.0 --port 8000
```

## Configuration

All settings are `WAX_`-prefixed environment variables. See
`src/wax/core/config.py` for the full schema. Key settings:

- `WAX_DATABASE_URL` — PostgreSQL (prod) or SQLite (dev)
- `WAX_SECRET_KEY` — master secret (required in production)
- `WAX_LLM_DEFAULT_PROVIDER` — `openai`, `anthropic`, or empty (mock)
- `WAX_OPENAI_API_KEY` / `WAX_ANTHROPIC_API_KEY` — provider keys
- `WAX_WHATSAPP_*` — WhatsApp Cloud API credentials
- `WAX_TERMINAL_TIMEOUT_SECONDS` — foreground process timeout (default 60)
- `WAX_TERMINAL_MAX_ROUNDS` — max terminal calls per message (default 10)
- `WAX_TERMINAL_WORKING_DIR_ROOT` — where execution workspaces live

## Testing

```bash
pytest tests/
```
