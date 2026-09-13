# WAX Runtime Boundary

**Status:** PROVISIONAL
**Last updated:** 2026-09-14

This document defines the explicit boundary around `wax.core` — the
universal runtime layer — and what may live outside it.

## The constitutional rule

From WAX Foundation §5 and Directive §5:

> **INFRASTRUCTURE, NOT INTELLIGENCE.**
> WAX should implement mechanisms.
> It should not implement the decisions those mechanisms exist to enable.

Translated into a structural rule:

> **`wax.core` contains no I/O.**

This is enforced as **Invariant INV-09** and tested in
`tests/architecture/test_core_boundary.py`.

## Layer model

```
┌─────────────────────────────────────────────────────────────┐
│  wax.interfaces.*  (WhatsApp adapter, Web adapter, future) │ ← adapters
├─────────────────────────────────────────────────────────────┤
│  wax.runtime       (FastAPI app, lifecycle, HTTP surface)   │ ← I/O
├─────────────────────────────────────────────────────────────┤
│  wax.state         (SQLAlchemy models, repositories)         │ ← I/O
│  wax.identity      (identity service)                       │ ← I/O
│  wax.authority     (authorization service)                  │ ← I/O (enforced)
│  wax.memory        (memory service)                         │ ← I/O
│  wax.capabilities   (capability registry + invocation)      │ ← I/O
│  wax.execution      (durable execution, workers)            │ ← I/O
│  wax.intelligence   (model provider adapters)               │ ← I/O
│  wax.isolation      (code execution boundaries — sandbox)   │ ← I/O
│  wax.media          (extraction pipeline)                   │ ← I/O
│  wax.observability  (structured logs, metrics, traces)      │ ← I/O
├─────────────────────────────────────────────────────────────┤
│  wax.core          (contracts, invariants, exceptions,       │ ← NO I/O
│                     config schema — pure data only)         │
└─────────────────────────────────────────────────────────────┘
```

## What wax.core may contain

- **Data classes and Pydantic models** that represent contracts (e.g., `WaxSettings`, identity contracts, capability contracts)
- **Enums** (e.g., `Environment`, `LogLevel`)
- **Exception definitions**
- **Invariant declarations** (declarative — no enforcement logic that touches I/O)
- **Pure validation functions** (no I/O side effects)
- **Type definitions and protocols**

## What wax.core may NOT contain

- **Database queries** (no `sqlalchemy`, `asyncpg`, `aiosqlite`)
- **HTTP requests** (no `httpx`, `aiohttp`, `requests`)
- **Logging** (no `structlog`, `logging` — core returns values, callers log)
- **File I/O** (no `open()`, no `aiofiles`)
- **Subprocess** (no `subprocess`, `asyncio.subprocess`)
- **ASGI/WSGI** (no `fastapi`, `starlette`, `uvicorn`)
- **External SDKs** (no `openai`, `anthropic`, etc.)
- **Domain concepts** (no `student`, `lesson`, `exam`, etc. — INV-01)

## Dependency direction

```
interfaces  →  runtime  →  state/identity/authority/...  →  core
                                                              ↑
                                              everything points inward
                                              nothing in core points outward
```

This is the standard "ports and adapters" / hexagonal dependency direction.
`wax.core` depends on nothing outside itself (except stdlib + pydantic for
data modeling).

## What happens when a boundary is violated

If `tests/architecture/test_core_boundary.py` fails:

1. The PR is blocked.
2. The violating import must be either:
   - **Moved out of core** (preferred — the I/O belongs in `wax.state`, `wax.runtime`, etc.)
   - **Justified as essential** (rare — requires a constitutional ADR amending INV-09)

There is no third option. "Just this once" is not a valid justification.

## Future considerations

- When Phase F (Memory) is built, it will need to decide whether the memory
  storage abstraction lives in `wax.core` (as a Protocol) and the
  implementation in `wax.memory`. **Yes** — this is the correct split.
- When Phase G (Capabilities) is built, capability *contracts* (Pydantic
  models) live in `wax.core.capabilities_contracts`; capability
  *implementations* (actual tool execution) live in `wax.capabilities`.
- When Phase O (Intelligence) is built, the LLM adapter Protocol lives in
  `wax.core.intelligence_contracts`; concrete adapters for OpenAI,
  Anthropic, etc. live in `wax.intelligence.adapters`.
