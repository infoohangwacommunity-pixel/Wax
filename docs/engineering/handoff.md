# WAX — Engineering Handoff

**Last updated:** 2026-09-13
**Current commit:** `da52c23` (Phase Q on main)
**Total tests:** 208 passing
**Total commits this session:** 9 (Phase A → Phase Q)

## Current Architecture

```
                    HUMAN
                      │
                      ▼
                INTERFACE(S)  ←── WhatsApp adapter (Phase Q, IMPLEMENTED)
                      │
                      ▼
              ┌───────────────┐
              │   WAX RUNTIME │  ←── FastAPI app, structured logging,
              │               │      graceful shutdown, /healthz, /readyz
              │ Identity      │  ←── Interface-independent (Phase D)
              │ State         │  ←── PostgreSQL/SQLite via SQLAlchemy 2.0 (Phase C)
              │ Memory        │  ←── Rich structure: provenance, confidence, expiry (Phase F)
              │ Authority     │  ←── Runtime-enforced, AI has zero perms (Phase E)
              │ Capabilities  │  ←── Registry + Invoker (Phase G)
              │ Execution     │  ←── Durable, resumable, crash-tested (Phase H)
              │ Isolation     │  ←── SubprocessBoundary with timeout + secret filter (Phase I)
              │ Resources     │  ←── BudgetAccountant, 8 resource kinds (Phase J)
              │ Intelligence  │  ←── Provider-agnostic, OpenAI + Mock adapters (Phase K)
              │ Objective     │  ←── Open-world, no hardcoded kinds (Phase L)
              │ Agency        │  ←── Approval gates, AI never executes directly (Phase M)
              │ Security      │  ←── Trust boundaries + prompt-injection defense (Phase N/O)
              │ Observability │  ←── Metrics + structured logs (Phase P)
              └───────────────┘
```

## Critical Invariants (enforced by tests)

| ID | Statement | Enforcement |
|---|---|---|
| INV-01 | Universal runtime mechanisms must not require education | `tests/architecture/test_core_boundary.py` |
| INV-02 | Core WAX must not depend on any specific interface | `tests/integration/test_identity.py::TestIdentitySurvivesInterfaceRemoval` |
| INV-03 | Core WAX must not depend on any specific model provider | `tests/integration/test_intelligence.py::TestProviderIsolationArchitecture` |
| INV-04 | AI-requested actions must pass through runtime authorization | `tests/integration/test_authority.py::test_ai_principal_has_no_permissions` |
| INV-05 | Important state must survive process interruption | Real crash-test passed (Phase H) |
| INV-06 | Security-sensitive actions must be attributable | Audit log enforced on every auth decision + agency decision |
| INV-07 | Major external dependencies must have a replacement boundary | Architecture (adapters isolate providers) |
| INV-08 | Unknown legitimate objectives must not require modifying the universal ontology | `tests/integration/test_objective.py::TestObjectiveIsOpenWorld` |
| INV-09 | wax.core must contain no I/O | `tests/architecture/test_core_boundary.py::TestCoreHasNoIO` |
| INV-10 | No mock may be claimed as production infrastructure | MockLLMProvider marked MOCK in metadata; NoopBoundary rejected by IsolationService |

## How to run tests

```bash
cd /home/z/my-project/wax
uv venv .venv
. .venv/bin/activate
uv pip install -e ".[dev]"
pytest                              # all 208 tests
pytest tests/integration            # integration tests only
pytest tests/architecture           # invariant enforcement
pytest -k whatsapp                  # by keyword
```

## How to apply migrations

```bash
# Local dev (SQLite):
WAX_DATABASE_URL=sqlite+aiosqlite:///./wax.db \
WAX_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(64))") \
alembic upgrade head

# Production (PostgreSQL):
WAX_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/wax \
WAX_SECRET_KEY=$WAX_SECRET_KEY \
alembic upgrade head

# Rollback:
alembic downgrade -1
```

## How to start the runtime locally

```bash
cp .env.example .env
# Edit .env: set WAX_SECRET_KEY to `python -c "import secrets; print(secrets.token_urlsafe(64))"`
. .venv/bin/activate
uvicorn wax.runtime.asgi:app --reload --host 127.0.0.1 --port 8000
```

Visit `http://127.0.0.1:8000/docs` for OpenAPI.

## How to deploy to Railway

1. Push to `main` — Railway auto-deploys via `railway.toml` config.
2. Set the following secrets in Railway's dashboard:
   - `WAX_SECRET_KEY` (generate with `python -c "import secrets; print(secrets.token_urlsafe(64))"`)
   - `WAX_DATABASE_URL` (from Railway's PostgreSQL add-on)
3. Railway runs `alembic upgrade head` then `uvicorn wax.runtime.asgi:app`.

## CI

`.github/workflows/ci.yml` runs on every push to main and every PR:
- Lint (ruff check)
- Format check (ruff format --check)
- Type check (mypy)
- Migrations round-trip on PostgreSQL 16 service container
- pytest with coverage

## Phases Status

| Phase | Status | What it does |
|---|---|---|
| A — Foundation | IMPLEMENTED | config, logging, health, lifecycle, test baseline |
| B — Runtime Boundary | IMPLEMENTED | layer model, architecture tests |
| C — State/Persistence | IMPLEMENTED | DB, migrations, audit log |
| D — Identity | IMPLEMENTED | interface-independent principal + credentials |
| E — Authority | IMPLEMENTED | runtime-enforced authorization, AI has zero perms |
| F — Memory | IMPLEMENTED | rich structure, NOT a vector DB |
| G — Capabilities | IMPLEMENTED | registry + invoker, INV-04 enforced |
| H — Execution | IMPLEMENTED | durable, resumable, crash-tested |
| I — Isolation | IMPLEMENTED | SubprocessBoundary with timeout + secret filter |
| J — Resources | IMPLEMENTED | BudgetAccountant, 8 resource kinds |
| K — Intelligence | IMPLEMENTED | provider abstraction, OpenAI + Mock adapters |
| L — Objective | IMPLEMENTED | open-world, no hardcoded kinds |
| M — Agency | IMPLEMENTED | approval gates, AI never executes directly |
| N/O — Security | IMPLEMENTED | trust boundaries + prompt-injection defense |
| P — Observability | IMPLEMENTED | metrics + structured logs |
| Q — Interfaces | IMPLEMENTED | WhatsApp adapter (text, media, templates, interactive, signature verify) |
| R — Dynamic Environments | PROPOSED | temporary workspaces (coding, document, analysis) |
| S — External Services | PROPOSED | connector framework (GitHub, Gmail, Drive, Spotify, Calendar, Browser) |
| T — Reliability | PROPOSED | retries, dead-letter handling, recovery |
| U — Privacy | PROPOSED | consent, export, deletion, retention |
| V — Open-World Validation | PROPOSED | stress-test with tutoring, business, coding, writing, research |
| W — Production Hardening | PROPOSED | load testing, PostgreSQL testing, restart testing |
| X — Education Layer (WaxPrep) | PROPOSED | learning evidence, mastery estimation, misconceptions, assessment |

## Open Questions

1. **PostgreSQL testing in CI** — current CI uses SQLite for unit tests; PostgreSQL service container is configured but migration tests are basic. Phase W needs full PostgreSQL reliability testing.
2. **WhatsApp credentials** — to actually receive WhatsApp messages, the founder must register a Meta Developer app and provide credentials. The adapter code is complete; only credentials are missing.
3. **Production LLM provider** — `WAX_OPENAI_API_KEY` is wired but not yet used end-to-end. Phase R (Runtime Composition) will wire Objective → Intelligence → Response.
4. **Phase R composition** — the WhatsApp adapter has a placeholder runtime_callback. Wiring it to Objective + Intelligence + Execution is the next major milestone.

## What NOT to change casually

- `wax.core.invariants` — these are constitutional. Adding a new invariant is fine; weakening one requires an ADR.
- `wax.authority.permissions.BUILTIN_ROLES["ai"]` — must remain empty. The AI never has inherent permissions.
- `wax.isolation.subprocess_boundary._prepare_command` env filter — this is the security boundary. Loosening it would leak secrets to subprocesses.
- `wax.runtime.logging._redact_sensitive` — must keep the sensitive-key list conservative. Adding to it is fine; removing keys is dangerous.

## What remains unfinished

- Phase R (Dynamic Environments) — temporary workspaces not yet built
- Phase S (External Services) — connector framework not yet built
- Phase T (Reliability) — retries/dead-letter not yet built (basic retry in tenacity but no dead-letter queue)
- Phase U (Privacy) — consent/export/deletion/retention APIs not yet built
- Phase V (Open-World Validation) — formal stress tests not yet run
- Phase W (Production Hardening) — load testing, restart testing not yet done
- Phase X (Education Layer / WaxPrep) — not yet started; depends on all runtime phases

## Next dependency

**Phase R — Dynamic Environments**: temporary workspaces for the AI to request.
The AI may say "I need a coding environment to test this Python code" — the
runtime provisions an isolated workspace, executes code via Phase I
(SubprocessBoundary), and returns the result. The AI does not create
infrastructure directly.

After Phase R: Phase S (External Services) — connector framework for GitHub,
Gmail, Drive, etc.
