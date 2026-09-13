# ADR-0001: Initial Technology Selection for WAX Foundation

- **Status:** ADOPTED
- **Date:** 2026-09-13
- **Decider:** Super Z (autonomous engineering agent)
- **Reversibility:** Moderately expensive (affects all downstream code, but core boundary abstractions can survive a language swap if contracts are kept)

## Context

WAX is a greenfield AI-native runtime. The repository is empty (see `docs/research/repository/current-state.md`). The WAX Master Execution Directive mandates the ordering:

> problem → requirements → mechanisms → alternatives → trade-offs → technology → implementation

and explicitly forbids starting from technology ("Never start with technology").

The directive also mandates that major technology decisions be recorded as ADRs containing: requirement, alternatives, trade-offs, security implications, operational implications, cost implications, failure characteristics, selected mechanism, reason, replacement strategy.

## Problem

WAX needs an implementation stack that can support:

1. **Async I/O at its core.** LLM calls, database access, external API calls, capability invocation — all are I/O-bound, often long-latency (10–60 s for LLM streams), and must run concurrently.
2. **Type-safe contracts.** The AI/Runtime Contract (§44) requires a precise, validated representation of what intelligence receives: identity, permissions, memory, capabilities, etc. Runtime capability contracts (§34) must be stable across implementation changes.
3. **Transactional durable state.** Persistence must survive process crashes (Invariant 5). Real database with real transactions, real migrations, real rollback.
4. **Background execution.** Long-running intelligence (§38, §39) requires durable workflows, checkpoints, resumability — cannot live inside a single HTTP request/response cycle.
5. **LLM provider ecosystem.** The intelligence layer (§42) must support multiple providers behind a stable adapter. The Python ecosystem is the strongest here.
6. **Test infrastructure.** The test pyramid (§85) requires unit / integration / contract / failure / security / architecture / e2e / open-world tests.
7. **Observability.** Structured logs, metrics, traces (§56) — production-grade.
8. **Replaceability.** Model, interface, capability, and database must all be replaceable (Invariants 3, 7). The stack itself must not become a constitutional commitment.

## Alternatives Considered

### A. Python 3.11+ (FastAPI + SQLAlchemy 2.0 async + Pydantic v2 + Alembic + pytest + structlog)

**Strengths:**
- Best LLM ecosystem of any language (OpenAI, Anthropic, Google, Mistral, local — all first-class Python SDKs)
- Mature async story (`asyncio`, `asyncpg`, `httpx`)
- Pydantic v2 gives compile-time-checked contracts with runtime validation
- SQLAlchemy 2.0 async + Alembic is the most mature Python DB story
- FastAPI gives async HTTP, OpenAPI auto-docs, dependency injection, type-safe handlers
- pytest is the most ergonomic test runner in any language
- structlog gives structured JSON logging out of the box
- Easy deployment (Railway, Fly.io, Docker, any Linux box)

**Weaknesses:**
- Slower raw CPU than Go/Rust (acceptable — WAX is I/O-bound, not CPU-bound)
- GIL limits true parallelism per process (mitigated by multi-process deployment + asyncio for I/O)
- Dynamic typing by default (mitigated by Pydantic v2 + mypy --strict)

### B. TypeScript / Node.js (Next.js + Prisma + tRPC)

**Strengths:**
- Single language across frontend and backend
- Strong typing via TypeScript
- Excellent web ecosystem

**Weaknesses:**
- Weaker LLM SDK ecosystem (most provider SDKs are Python-first, JS ports lag)
- Prisma's migration story is weaker than Alembic's
- Single-threaded event loop is more limiting than asyncio for CPU-light I/O work
- Next.js couples runtime to web framework — but WAX runtime is not a web app

### C. Go (net/http + sqlc + pgx)

**Strengths:**
- Excellent runtime characteristics, single static binary
- Strong concurrency primitives
- Best deployment story of any language

**Weaknesses:**
- LLM SDK ecosystem is third-class (community ports, often stale)
- Slower iteration cycle
- Weaker ecosystem for AI/ML work

### D. Rust (axum + sqlx + tokio)

**Strengths:**
- Best safety and performance of any option
- Excellent async story
- Strong type system

**Weaknesses:**
- LLM ecosystem essentially nonexistent
- Slowest iteration cycle
- Overkill for an I/O-bound runtime

## Trade-offs

| Criterion | Python | TS | Go | Rust |
|---|---|---|---|---|
| LLM ecosystem | ★★★★★ | ★★★ | ★★ | ★ |
| Async I/O | ★★★★ | ★★★★ | ★★★★★ | ★★★★★ |
| Type safety | ★★★★ | ★★★★★ | ★★★★ | ★★★★★ |
| DB migrations | ★★★★★ | ★★★ | ★★★★ | ★★★★ |
| Test infrastructure | ★★★★★ | ★★★★ | ★★★★ | ★★★ |
| Iteration speed | ★★★★★ | ★★★★ | ★★★ | ★★ |
| Deployment | ★★★★ | ★★★★ | ★★★★★ | ★★★★★ |
| Team familiarity (presumed) | ★★★★★ | ★★★ | ★★ | ★ |

Python wins on the criteria that matter most for an AI-native runtime: LLM ecosystem, type-safe contracts (Pydantic), mature DB story, and iteration speed. Its weaknesses (raw CPU, GIL) are not on the critical path for WAX.

## Security Implications

- Python's runtime is mature and well-audited. Major vulnerabilities are typically in third-party packages, not the interpreter.
- Dependency surface must be kept small and pinned. `uv` lockfile will be committed.
- Secrets handling via `pydantic-settings` reading from environment, never from files committed to the repo.
- Sandboxed execution (future Phase M) will use OS-level isolation (containers, microVMs), not Python-level sandboxing — language choice does not constrain this.

## Operational Implications

- Deployment: container image (Docker) running `uvicorn` or `gunicorn -k uvicorn.workers.UvicornWorker`.
- Health: FastAPI `/healthz` endpoint.
- Observability: `structlog` for JSON logs, OpenTelemetry-compatible tracing (future), Prometheus metrics (future).
- Migrations: `alembic upgrade head` at deploy time, with rollback procedure documented.

## Cost Implications

- Python runtime is free.
- All chosen libraries are open-source (MIT/Apache/BSD).
- Hosting: Railway / Fly.io / any container host. No language-specific licensing.

## Failure Characteristics

- Process crash: state must be in PostgreSQL, not in-process. Mitigated by Phase C (State/Persistence).
- Slow LLM call: async means other requests are not blocked. Timeouts enforced via `httpx` and `asyncio.wait_for`.
- DB connection exhaustion: connection pool with bounded size, idle timeout.
- Migration failure: Alembic supports rollback; migrations tested against disposable DB before deploy.

## Selected Mechanism

- **Language:** Python 3.11+
- **Package manager:** `uv` (modern, fast, deterministic)
- **HTTP framework:** FastAPI 0.110+
- **Validation:** Pydantic v2
- **DB ORM:** SQLAlchemy 2.0 (async mode)
- **DB driver:** asyncpg
- **Migrations:** Alembic
- **Testing:** pytest + pytest-asyncio + httpx (for ASGI testing)
- **Logging:** structlog
- **HTTP client:** httpx
- **Configuration:** pydantic-settings

## Reason

Python is the strongest fit for an AI-native runtime where the dominant I/O pattern is "call LLM, persist state, return response." The LLM ecosystem, type-safe contract story (Pydantic v2), mature async DB story (SQLAlchemy 2.0 + asyncpg + Alembic), and ergonomics (pytest) together outweigh the raw-performance advantages of Go/Rust, because WAX is not CPU-bound.

## Replacement Strategy

If Python proves insufficient:

- **To Go:** Core contracts (Pydantic models) translate to Go structs; SQLAlchemy models to sqlc queries; FastAPI handlers to net/http handlers. The WAX core boundary (Phase B) insulates the migration — adapters and interfaces change, core contracts stay.
- **To Rust:** Higher migration cost due to ownership model. Same boundary applies.

The architectural boundary between `wax.core` (no I/O, pure contracts and invariants) and `wax.runtime` / `wax.state` / `wax.intelligence` (I/O implementations) makes a future language swap tractable: the core can be re-implemented in another language and the I/O layer re-bound.

## Validation Method

- All technology choices will be validated by passing tests in Phase A.
- Architecture invariant tests will verify that the core boundary is respected (no I/O imports in `wax.core`).
- A future Phase AC (Model Independence Test) will validate that the LLM provider adapter is genuinely replaceable.

## Follow-up

- ADR-0002 will record the database selection (PostgreSQL — preliminary decision).
- ADR-0003 will record the deployment target (Railway vs Fly.io — deferred until Phase Z / Production Hardening).
