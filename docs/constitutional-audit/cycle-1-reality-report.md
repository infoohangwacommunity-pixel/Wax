# WAX — Reality Report (Article 5 Reconstruction)

_Generated: 2026-09-14T17:57:49.938434+00:00_

This report reconstructs the repository's actual state from filesystem evidence (Law 5: Evidence Before Claims). It is the foundation for every downstream audit, ADR, and cycle report in this engineering cycle.

## 1. Git reality

| Field | Value |
|---|---|
| branch | `main` |
| sha | `4224bf9dc9463cb461f1df1be3f21896f73ed0a0` |
| short_sha | `4224bf9` |
| status | `(clean)` |
| origin_url | `https://github.com/infoohangwacommunity-pixel/Wax.git` |
| head_vs_origin | `0	0` |
| last_commit_subject | `docs: worklog POST-OMEGA-2 (memory re-audit dispositions)` |
| last_commit_date | `2026-09-14T14:53:32Z` |

## 2. File counts

### Actual counts (from filesystem)

| Kind | Count |
|---|---|
| Source files (src/wax/**/*.py) | 124 |
| Test files (tests/**/*.py) | 61 |
| Migration files (migrations/versions/*.py) | 12 |
| ADR files (docs/decisions/ADR-*.md) | 28 |

### Drift from mission document's claimed counts

| Kind | Claimed | Actual | Status |
|---|---|---|---|
| test_files | 63 | 61 | **DRIFT** |
| migration_files | 13 | 12 | **DRIFT** |
| adr_files | 34 | 28 | **DRIFT** |

> ⚠️ The mission document (Section 2.3) carries stale counts. Per Article 13 (Count Reconciliation), historical numbers may remain in historical sections, but current-state documents must use current counts.

### Source files by subsystem

| Subsystem | Files |
|---|---|
| __init__.py | 1 |
| agency | 3 |
| authority | 7 |
| capabilities | 9 |
| continuity | 5 |
| core | 4 |
| execution | 3 |
| identity | 4 |
| intelligence | 8 |
| interfaces | 5 |
| isolation | 5 |
| media | 5 |
| memory | 5 |
| objective | 5 |
| observability | 4 |
| reliability | 4 |
| resources | 3 |
| runtime | 20 |
| security | 7 |
| state | 17 |

### Test files by category

| Category | Files |
|---|---|
| architecture | 3 |
| conftest.py | 1 |
| evaluation | 1 |
| integration | 45 |
| unit | 11 |

## 3. Dependency audit

- Runtime dependencies declared: 16
- Optional dependency groups: 14
- Installed packages in environment: 5

### Runtime dependencies

- fastapi>=0.115.0
- uvicorn[standard]>=0.30.0
- asgiref>=3.8.0
- pydantic>=2.9.0
- pydantic-settings>=2.5.0
- sqlalchemy[asyncio]>=2.0.35
- alembic>=1.13.0
- aiosqlite>=0.20.0
- asyncpg>=0.29.0
- httpx>=0.27.0
- structlog>=24.4.0
- python-dotenv>=1.0.1
- anyio>=4.4.0
- tenacity>=9.0.0
- python-ulid>=2.7.0
- pyyaml>=6.0.2

### Optional dependency groups

- **exact-tokens**: 
- **media-docs**: 
- **dev**: pytest>=8.3.0, pytest-asyncio>=0.24.0, pytest-cov>=5.0.0, httpx>=0.27.0, ruff>=0.6.0, mypy>=1.11.0
- **requires**: 
- **packages**: 
- **testpaths**: 
- **pythonpath**: 
- **addopts**: -ra, --strict-markers, --strict-config, -q
- **markers**: unit: mechanism-level tests, integration: subsystem interaction tests, contract: boundary contract tests, architecture: architectural invariant tests, failure: failure-path tests, security: adversarial tests, e2e: end-to-end journey tests, open_world: unanticipated-objective tests
- **filterwarnings**: error, ignore::DeprecationWarning, ignore::PendingDeprecationWarning
- **src**: 
- **select**: E, W, F, I, B, UP, SIM, RUF
- **ignore**: E501, B008
- **module**: 

## 4. Architecture graph (subsystem seams)

Each row lists the subsystems that this subsystem *imports from*.

| Subsystem | Imports from |
|---|---|
| **agency** | authority, runtime |
| **authority** | agency, core, identity, objective, observability, runtime, state |
| **capabilities** | authority, core, identity, isolation, memory, objective, observability, resources, runtime, security, state |
| **continuity** | execution, memory, objective, runtime, state |
| **execution** | runtime, state |
| **identity** | runtime, state |
| **intelligence** | core, observability, reliability, runtime |
| **interfaces** | core, runtime |
| **isolation** | core, observability, runtime |
| **media** | runtime |
| **memory** | observability, runtime, state |
| **objective** | authority, core, runtime, state |
| **observability** | runtime, state |
| **reliability** | core, state |
| **resources** | runtime |
| **runtime** | agency, authority, capabilities, continuity, core, execution, identity, intelligence, interfaces, memory, objective, observability, reliability, resources, security, state |
| **security** | runtime |
| **state** | core, runtime |

### Subsystem-level orphans (no other subsystem imports them)

- `media`

> These may be entry points (interfaces/, runtime/) or genuinely unwired subsystems. Investigate per Article 6.

## 5. Dead code / unwired module scan

- Total candidate unwired module files: **3**

<details><summary>Unwired module files (click to expand)</summary>

- src/wax/core/invariants.py  (parent imported; specific module not directly imported)
- src/wax/runtime/asgi.py  (parent imported; specific module not directly imported)
- src/wax/security/trust.py  (parent imported; specific module not directly imported)

</details>

## 6. TODO / FIXME / HACK scan

- Total findings: **3**

<details><summary>Findings (click to expand)</summary>

| File | Line | Marker | Text |
|---|---|---|---|
| src/wax/runtime/app.py | 72 | TODO | `# RuntimeServices container (Phase G — the TODO that marked the edge` |
| src/wax/runtime/app.py | 140 | TODO | `# (The old "TODO Phase G" comment — the audit's marker of where the` |
| src/wax/runtime/services.py | 4 | TODO | `a single comment in the lifespan: "TODO Phase G: initialize capability` |

</details>

## 7. Hardcoded brand names in src/wax/core + src/wax/runtime

- Total findings: **54**

<details><summary>Findings (capped at 200)</summary>

| File | Line | Brand | Text |
|---|---|---|---|
| src/wax/core/invariants.py | 56 | whatsapp | `statement="Core WAX must not depend on any specific interface (WhatsApp, web, etc.).",` |
| src/wax/core/invariants.py | 59 | whatsapp | `"If WhatsApp disappears, WAX must still exist as a runtime. "` |
| src/wax/core/config.py | 173 | github | `acquisition_allowed_hosts: str = "files.pythonhosted.org,github.com,objects.githubusercontent.com,raw.githubusercontent.com,registry.npmjs.org"` |
| src/wax/runtime/__init__.py | 5 | whatsapp | `(WhatsApp, Web, future interfaces) connect into.` |
| src/wax/runtime/delivery.py | 3 | whatsapp | `Phase W (Interface Intelligence) requires that the runtime — not WhatsApp —` |
| src/wax/runtime/delivery.py | 9 | whatsapp | `The router knows NOTHING about WhatsApp. It knows about interface kinds,` |
| src/wax/runtime/delivery.py | 17 | telegram | `Adding Telegram/web means registering another sender (with its own policy,` |
| src/wax/runtime/services.py | 14 | whatsapp | `Nothing here knows about domains, education, WhatsApp, or any use case —` |
| src/wax/runtime/logging.py | 45 | openai | `"sk-",  # OpenAI-style API keys` |
| src/wax/runtime/app.py | 10 | whatsapp | `- The HTTP surface is one of potentially many interfaces (WhatsApp, web,` |
| src/wax/runtime/app.py | 11 | telegram | `Telegram, ...). Nothing in `wax.core` may know about HTTP.` |
| src/wax/runtime/app.py | 99 | whatsapp | `from wax.interfaces.whatsapp.client import WhatsAppClient` |
| src/wax/runtime/app.py | 113 | whatsapp | `lifecycle.on_shutdown("whatsapp.close", wa_client.close())` |
| src/wax/runtime/app.py | 121 | whatsapp | `"whatsapp",` |
| src/wax/runtime/app.py | 134 | whatsapp | `"whatsapp.client.initialized", phone_number_id=settings.whatsapp_phone_number_id` |
| src/wax/runtime/app.py | 138 | whatsapp | `log.warning("whatsapp.client.not_configured")` |
| src/wax/runtime/app.py | 259 | whatsapp | `whatsapp = getattr(app.state, "whatsapp_client", None)` |
| src/wax/runtime/app.py | 261 | whatsapp | `checks["whatsapp"] = "ok" if whatsapp is not None else "fail: not_initialized"` |
| src/wax/runtime/app.py | 261 | whatsapp | `checks["whatsapp"] = "ok" if whatsapp is not None else "fail: not_initialized"` |
| src/wax/runtime/app.py | 263 | whatsapp | `checks["whatsapp"] = "not_configured"` |
| src/wax/runtime/app.py | 375 | whatsapp | `@app.get("/webhooks/whatsapp", tags=["webhook"])` |
| src/wax/runtime/app.py | 383 | whatsapp | `Meta sends: GET /webhooks/whatsapp?hub.mode=subscribe` |
| src/wax/runtime/app.py | 395 | whatsapp | `from wax.interfaces.whatsapp.adapter import WhatsAppAdapter` |
| src/wax/runtime/app.py | 404 | whatsapp | `@app.post("/webhooks/whatsapp", tags=["webhook"])` |
| src/wax/runtime/app.py | 409 | whatsapp | `"""Receive WhatsApp webhook events.` |
| src/wax/runtime/app.py | 420 | whatsapp | `the WhatsApp app secret.` |
| src/wax/runtime/app.py | 428 | whatsapp | `from wax.interfaces.whatsapp.adapter import WhatsAppAdapter` |
| src/wax/runtime/app.py | 433 | whatsapp | `"""Convert the WhatsApp message → RuntimeRequest → process via bridge.` |
| src/wax/runtime/app.py | 435 | whatsapp | `This is the SOLE place where WhatsApp shapes become RuntimeRequest.` |
| src/wax/runtime/app.py | 451 | whatsapp | `log.error("whatsapp.bridge_not_configured")` |
| src/wax/runtime/app.py | 456 | whatsapp | `interface_kind=InterfaceKind.WHATSAPP,` |
| src/wax/runtime/app.py | 483 | whatsapp | `"whatsapp.duplicate_message.skipped",` |
| src/wax/runtime/app.py | 490 | whatsapp | `"whatsapp.message.not_replied",` |
| src/wax/runtime/app.py | 514 | whatsapp | `svc.metrics.send_failure("whatsapp")` |
| src/wax/runtime/app.py | 530 | whatsapp | `"whatsapp.send_failure.unresolvable_principal",` |
| src/wax/runtime/app.py | 546 | whatsapp | `interface_kind="whatsapp",` |
| src/wax/runtime/app.py | 566 | whatsapp | `svc.metrics.delivery_retrying("whatsapp")` |
| src/wax/runtime/app.py | 569 | whatsapp | `"whatsapp.delivery_record_write_failed",` |
| src/wax/runtime/bridge/__init__.py | 3 | whatsapp | `This is the universal entrypoint that ANY interface adapter (WhatsApp,` |
| src/wax/runtime/bridge/__init__.py | 4 | telegram | `web, Telegram, future) calls to submit a user message to the runtime.` |
| src/wax/runtime/bridge/__init__.py | 18 | whatsapp | `WhatsApp-specific shapes.` |
| src/wax/runtime/bridge/contracts.py | 3 | whatsapp | `These are interface-agnostic. A WhatsApp message, a web chat message,` |
| src/wax/runtime/bridge/contracts.py | 4 | telegram | `and a future Telegram message all become a RuntimeRequest before they` |
| src/wax/runtime/bridge/contracts.py | 5 | whatsapp | `reach the runtime. The runtime never sees WhatsApp-specific shapes.` |
| src/wax/runtime/bridge/contracts.py | 8 | whatsapp | `the runtime only knows about RuntimeRequest, never about WhatsApp.` |
| src/wax/runtime/bridge/contracts.py | 27 | whatsapp | `WHATSAPP = "whatsapp"` |
| src/wax/runtime/bridge/contracts.py | 27 | whatsapp | `WHATSAPP = "whatsapp"` |
| src/wax/runtime/bridge/contracts.py | 29 | telegram | `TELEGRAM = "telegram"` |
| src/wax/runtime/bridge/contracts.py | 29 | telegram | `TELEGRAM = "telegram"` |
| src/wax/runtime/bridge/contracts.py | 55 | whatsapp | `For example, the WhatsApp adapter converts a WhatsAppIncomingMessage` |
| src/wax/runtime/bridge/contracts.py | 59 | whatsapp | `- interface_message_id: the platform's message ID (e.g. WhatsApp wamid.*)` |
| src/wax/runtime/bridge/contracts.py | 62 | whatsapp | `- interface_kind: which interface this came from (whatsapp, web, ...)` |
| src/wax/runtime/bridge/contracts.py | 64 | whatsapp | `(e.g. WhatsApp phone number, web session token)` |
| src/wax/runtime/bridge/contracts.py | 107 | whatsapp | `before sending. For WhatsApp, the response.text becomes a text message;` |

</details>

_Note: Inspect each finding manually; some are legitimate (e.g. module names like `openai_provider` are in adapters, not scanned here)._

## 8. Snapshot: ADR inventory

- `docs/decisions/ADR-0001-initial-technology-selection.md`
- `docs/decisions/ADR-0002-database-selection.md`
- `docs/decisions/ADR-0003-wiring-unwired-subsystems.md`
- `docs/decisions/ADR-0004-durable-work-runtime.md`
- `docs/decisions/ADR-0005-dynamic-provisioning.md`
- `docs/decisions/ADR-0006-code-execution-authority.md`
- `docs/decisions/ADR-0007-interface-intelligence.md`
- `docs/decisions/ADR-0008-network-boundary.md`
- `docs/decisions/ADR-0009-provider-resilience.md`
- `docs/decisions/ADR-0010-memory-mechanisms.md`
- `docs/decisions/ADR-0011-durable-waiting.md`
- `docs/decisions/ADR-0012-memory-and-context.md`
- `docs/decisions/ADR-0013-multi-worker-runtime.md`
- `docs/decisions/ADR-0014-human-approval-primitive.md`
- `docs/decisions/ADR-0015-acquisition-retention-context-negotiation.md`
- `docs/decisions/ADR-0016-namespace-code-isolation.md`
- `docs/decisions/ADR-0017-maintenance-leader-election.md`
- `docs/decisions/ADR-0018-adapter-token-accounting.md`
- `docs/decisions/ADR-0019-memory-retrieval-upgrade.md`
- `docs/decisions/ADR-0020-objective-lifecycle-completion.md`
- `docs/decisions/ADR-0021-durable-delivery.md`
- `docs/decisions/ADR-0022-memory-links-importance-observation.md`
- `docs/decisions/ADR-0023-context-sections-artifacts.md`
- `docs/decisions/ADR-0024-provider-failover.md`
- `docs/decisions/ADR-0025-retention-decision-boundaries.md`
- `docs/decisions/ADR-0026-open-capability-registry-future-model.md`
- `docs/decisions/ADR-0027-multi-server-state.md`
- `docs/decisions/ADR-0028-capability-invocation-idempotency.md`

## 9. Snapshot: Migration inventory

- `migrations/versions/a06446a7edd3_phase_v_reliability.py`
- `migrations/versions/a8c2e6f0b4d6_capability_invocation_idempotency.py`
- `migrations/versions/b7f21c9d4e02_phase_y_durable_work.py`
- `migrations/versions/b9c1d3e5f7a2_objective_execution_history.py`
- `migrations/versions/c3a95f1e8b21_phase_z_provisioning.py`
- `migrations/versions/c4d6e8f0a2b3_delivery_records.py`
- `migrations/versions/d6f8a2b4c9e1_memory_links_metadata.py`
- `migrations/versions/d9e4f2a8b1c7_memory_retrieval_upgrade.py`
- `migrations/versions/e1a3c5e7b9d2_artifact_records.py`
- `migrations/versions/e5c2a9f47b61_phase_wait_conditions.py`
- `migrations/versions/f2b4d6a8c0e2_approval_creation_idempotency.py`
- `migrations/versions/f8d3b7a9c1e4_approval_primitive.py`

## 10. Honest limitations of this report

- This report reconstructs filesystem reality only. It does *not* execute tests, migrations, or the live probe — those are separate evidence streams produced by later steps of Cycle 1.
- The architecture graph uses `ast`-level import analysis. Dynamic imports or string-based dispatch will not appear.
- The brand-leakage scan uses a naive regex; expect false positives where adapter brand names appear in legitimate test fixtures or docstrings.
- 'Unwired module' is heuristic. Some files are entry points (`__main__`, ASGI entry) and will look unwired by design. Each finding requires human review per Article 6.
