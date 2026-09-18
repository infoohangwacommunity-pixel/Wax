# WAX

WAX is an open-world AI runtime. It provides intelligence with:

- **a terminal environment** — the universal interface to the world
- **memory** — continuity across time
- **persistent execution** — what happened, what's happening
- **durable work** — responsibilities that survive restarts
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

## Architecture

```
HUMAN
  ↓
CONVERSATION
  ↓
DURABLE WORK
  ↓
EXECUTION
  ↓
INTELLIGENCE
  ↕
TERMINAL / ENVIRONMENT
  ↕
REAL WORLD SERVICES
  ↓
CHECKPOINT / MEMORY / STATE
  ↓
NEXT EXECUTION
```

The loop: **remember → understand → act → observe → learn → continue**.

## What's here

```
src/wax/
  core/           config, exceptions, invariants
  identity/       principals + credentials + phone normalization
  intelligence/   LLM providers (any OpenAI-compatible endpoint) + resilience
  interfaces/     WhatsApp adapter
  memory/         memory records + retrieval + lifecycle + extraction + consolidation
  execution/      execution history + steps + recovery
  continuity/     conversation threading + context assembly + context intelligence
  runtime/
    app.py         FastAPI factory
    asgi.py        ASGI entrypoint
    services.py    process-wide service container
    executor.py    the terminal executor (core of the open-world architecture)
    bridge/        the intelligence ↔ terminal loop
    work/          durable work + reentry + runner
    delivery.py    outbound delivery router
    delivery_queue.py  retry-able delivery records
    maintenance.py lifecycle hygiene
    rate_limit.py  per-principal rate limiting
    cost_protection.py  per-principal daily spend cap
    web_pages.py   DB-backed interaction sessions
    lifecycle.py   signal handlers + shutdown
    logging.py     structured logging
  state/          SQLAlchemy models + engine
  observability/  audit events
  reliability/    retries + circuit breakers
src/wax_runtime/  helper module for the AI (schedule, remember, recall, serve_page)
```

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

All settings are `WAX_`-prefixed environment variables. Key settings:

- `WAX_DATABASE_URL` — PostgreSQL (prod) or SQLite (dev)
- `WAX_SECRET_KEY` — master secret (required in production)
- `WAX_LLM_DEFAULT_PROVIDER` — `openai` (any OpenAI-compatible endpoint)
- `WAX_LLM_API_KEY` — API key for the primary provider
- `WAX_LLM_BASE_URL` — provider API URL (Groq, Together, OpenRouter, etc.)
- `WAX_LLM_MODEL` — model name
- `WAX_LLM_PROVIDER_FALLBACKS` — comma-separated fallback configs
- `WAX_WHATSAPP_*` — WhatsApp Cloud API credentials
- `WAX_PUBLIC_URL` — public URL for interaction sessions
- `WAX_TERMINAL_*` — terminal executor settings
- `WAX_RATE_LIMIT_MESSAGES_PER_HOUR` — per-user rate limit (default 30)
- `WAX_DAILY_COST_BUDGET_CENTS` — per-user daily spend cap (default 500)

## Provider-agnostic

The runtime is NOT hardcoded to any provider. Any OpenAI-compatible
endpoint works (Groq, Together, OpenRouter, Mistral, vLLM, Ollama, etc.)
without code changes.

Example for Groq:
```
WAX_LLM_DEFAULT_PROVIDER=openai
WAX_LLM_API_KEY=gsk_your_key
WAX_LLM_BASE_URL=https://api.groq.com/openai/v1
WAX_LLM_MODEL=llama-3.1-8b-instant
```

## Testing

```bash
pytest tests/
```
