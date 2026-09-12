# WAX — Engineering Worklog

Shared, append-only multi-agent work log. Newest entries at the bottom.
Format: `---` separator, then Task ID / Agent / Task / Work Log / Stage Summary.

---
Task ID: 0
Agent: main (Super Z)
Task: Reconnaissance — clone repository, inspect state, read all foundation/directive documents, produce current-state report.

Work Log:
- Cloned https://github.com/infoohangwacommunity-pixel/Wax.git into /home/z/my-project/wax/
- Inspected Git state: 3 commits, single `main` branch, no remotes beyond origin
- Inspected file tree: README.md (5 bytes — "# Wax"), WAX_Project_Foundation_Notes.pdf (50 KB, 10 pages), "architectural research guidelines.md" (888 lines, Stages 0–11 research roadmap, no implementation)
- No source code, no tests, no migrations, no CI, no package manifest
- Read all 3 uploaded foundation/directive documents in prior turns (5,061 + 3,384 + 10 PDF pages)
- Read first 200 lines of research guidelines doc to confirm it is a research roadmap, not engineering
- Created directory structure: docs/{architecture,research/repository,decisions,engineering,security,operations}, src/wax/{core,runtime,state,identity,authority,memory,capabilities,execution,intelligence,interfaces,observability}, migrations/versions, tests/{unit,integration,contract,architecture,e2e}, scripts
- Wrote current-state report to docs/research/repository/current-state.md
- Wrote ADR-0001 (technology selection) — chose Python + FastAPI + SQLAlchemy 2.0 async + Pydantic v2 + Alembic + pytest + structlog
- Wrote ADR-0002 (database selection) — PostgreSQL for production, SQLite for tests

Stage Summary:
- Repository is essentially empty. This is a fresh build, not a migration.
- No existing code to preserve or refactor.
- Three foundational documents converge on the same architectural vision: WAX = AI-native environment/runtime; infrastructure, not intelligence; AI has agency but not sovereignty; open-world; anti-ontology; model/interface/capability must be replaceable.
- Engineering decision: build from scratch in Python (FastAPI + SQLAlchemy 2.0 async + Pydantic v2 + Alembic + pytest + structlog).
- Next dependency: Phase A (Foundation) — project structure, config, logging, health, graceful shutdown, test infrastructure.

---
Task ID: A
Agent: main (Super Z)
Task: Phase A — Foundation. Build runtime, package management, config, secrets, logging, health, graceful shutdown, test baseline.

Work Log:
- Wrote pyproject.toml with all dependencies (FastAPI, SQLAlchemy 2.0 async, Pydantic v2, Alembic, structlog, httpx, pytest, etc.) + ruff + mypy config
- Wrote .gitignore (Python, venv, env files, sqlite DBs, IDE)
- Wrote .env.example (WAX_ENV, WAX_DATABASE_URL, WAX_SECRET_KEY, LLM keys — all empty)
- Built wax.core (no I/O package):
  - exceptions.py: WaxError hierarchy (10 exception types)
  - config.py: WaxSettings (pydantic-settings), Environment/LogLevel/LogFormat enums, load_settings(), settings_for_testing()
  - invariants.py: 10 declared invariants (INV-01 through INV-10) with id, statement, reason, enforcement, status
- Built wax.runtime (I/O package):
  - logging.py: structlog setup with secret redaction (sk-, Bearer, password, secret, token, api_key, etc.)
  - lifecycle.py: LifecycleManager with signal handlers, LIFO shutdown, bounded timeout per handler
  - app.py: FastAPI factory with lifespan (startup logging, shutdown via LifecycleManager), /healthz, /readyz, / endpoints
  - asgi.py: module-level app for uvicorn
- Wrote tests:
  - conftest.py: test_settings, app, client fixtures
  - unit/test_config.py: 13 tests (env validation, production hardening, predicates, load_settings)
  - unit/test_logging.py: 10 tests (redaction of 8 sensitive key types, JSON vs console format)
  - unit/test_invariants.py: 5 tests (tuple structure, unique IDs, lookup)
  - integration/test_health.py: 5 tests (root, healthz, readyz, 404, openapi)
  - architecture/test_core_boundary.py: 3 tests (no I/O imports, no domain concepts)
- Created venv via uv, installed all dependencies including dev extras
- Ran tests: 42 passing
- Committed as "Phase A — Foundation"

Stage Summary:
- Phase A is IMPLEMENTED. Minimal WAX process can: start, stop, report health, load configuration safely, run tests, log structured events with secret redaction, gracefully shut down on SIGINT/SIGTERM.
- Architecture tests enforce INV-01 (no education concepts in wax.core identifiers) and INV-09 (no I/O imports in wax.core).
- Production hardening: missing WAX_SECRET_KEY, short secret, or SQLite in production all raise WaxConfigurationError.
- All 42 tests pass.
- Next dependency: Phase B (Runtime Boundary) — explicit docs + Phase C (State/Persistence).

---
Task ID: B-C-D-E
Agent: main (Super Z)
Task: Phases B, C, D, E — Runtime Boundary, State/Persistence, Identity, Authority.

Work Log:
- Phase B (Runtime Boundary):
  - Wrote docs/architecture/runtime-boundary.md — explicit layer model:
    interfaces → runtime → {state, identity, authority, memory, ...} → core
  - Documented what wax.core may/may not contain
  - Documented dependency direction (everything points inward; core points outward to nothing)
  - Documented violation response (PR blocked; either move I/O out or justify via ADR)

- Phase C (State/Persistence):
  - wax.state/models.py: SQLAlchemy 2.0 declarative Base + ULIDPrimaryKeyMixin + TimestampMixin
  - wax.state/engine.py: async engine creation, session factory, db_session() context manager with rollback on exception
  - wax.state/audit_models.py: AuditEvent (append-only, INV-06)
  - alembic.ini + migrations/env.py: Alembic config that reads WAX_DATABASE_URL from env
  - Generated first migration `c112c70ee393_phase_cde_state_identity_authority.py`
  - Tested migration: upgrade applies cleanly, downgrade reverses (reversibility verified)
  - Real persistence test: created principal, disposed engine, re-initialized, recovered principal — state survived
  - Tests: 7 state integration tests (engine lifecycle, schema creation, session rollback)

- Phase D (Identity — interface-independent):
  - wax.state/identity_models.py: Principal (universal, no interface-specific cols) + PrincipalCredential (kind-discriminated)
  - wax.identity/contracts.py: Pydantic models for API surface; ALLOWED_CREDENTIAL_KINDS enum
  - wax.identity/repository.py: PrincipalRepository (create, get, soft_delete, add_credential, find_credential, resolve_principal_by_credential, list_credentials)
  - resolve_principal_by_credential() is the core of interface-independent identity: an interface adapter (e.g. WhatsApp webhook) resolves its identifier to a universal principal via this method
  - Tests: 12 identity integration tests including the critical "principal survives interface credential removal" test (simulates WhatsApp disappearing)

- Phase E (Authority — runtime-enforced, not model-enforced):
  - wax.state/authority_models.py: Role (with JSON permissions array) + PrincipalRole (assignment table)
  - wax.authority/permissions.py: BUILTIN_PERMISSIONS (15 permissions), BUILTIN_ROLES (admin/member/service/ai), is_permission_granted() with wildcard matching
  - wax.authority/service.py: AuthorizationService — the SOLE point where "may this principal do X?" is answered
  - Every check() writes an audit record (INV-06 enforced)
  - The "ai" role has ZERO permissions — INV-04 (AI is untrusted requester; model cannot grant itself authority by producing text)
  - Anonymous requests (principal_id=None) denied by default
  - Permission matching supports wildcards: "capability.invoke:any" matches "capability.invoke:web_search"; "admin.*" matches "admin.role.assign"
  - Tests: 8 authority integration tests including the critical "AI principal has no permissions" test (loops through all 15 BUILTIN_PERMISSIONS, asserts all denied)

- Updated wax.runtime/app.py to:
  - Initialize DB engine in lifespan, register dispose_engine() on shutdown
  - /readyz now checks DB connectivity (SELECT 1)
- Updated tests/conftest.py app fixture to initialize engine + create schema

Issues encountered and fixed:
- Architecture test initially flagged __future__ import (stdlib, allowed — added to allowed set)
- Architecture test initially flagged 'WaxPrep' string in invariant documentation (legitimate — switched to AST-based identifier check that ignores docstrings/comments)
- db_session() was an async generator without @asynccontextmanager — added decorator
- JSONB type not portable to SQLite — switched to generic JSON type
- Role.permissions relationship with association table failed (str target) — switched to JSON array column on Role
- Role.permissions was None at construction time before flush — added __init__ override to default to []
- Test client fixture did not initialize DB engine — added init_engine + create_all

Stage Summary:
- Phase B: IMPLEMENTED (boundary docs + architecture tests)
- Phase C: IMPLEMENTED (state, persistence, migrations — tested reversible + real durability)
- Phase D: IMPLEMENTED (interface-independent identity — verified by interface-removal test)
- Phase E: IMPLEMENTED (runtime-enforced authorization, AI has no inherent permissions, audit logged)
- 75 tests pass (14 unit + 5 architecture + 56 integration/contract)
- Committed as "Phase B+C+D+E — Runtime Boundary, State, Identity, Authority"
- Next dependency: Phase F — Memory (real memory infrastructure, not vector DB)
