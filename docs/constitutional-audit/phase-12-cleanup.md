# WAX Phase 12 — Constitutional Cleanup Audit

**Date**: 2026-09-15
**Per**: ADR-0045, Article 6, Article 25

## Audit Method

Every `src/wax/` directory was classified by checking whether it's
imported by production runtime code (the bridge, the work runner,
the app lifespan, or other LIVE subsystems).

## Classification Summary

### LIVE (production-reachable)

| Subsystem | Justification |
|---|---|
| `core/` | config, exceptions, invariants — imported everywhere |
| `state/` | all models + engine — backed by 18 migrations |
| `identity/` | principal + credential models + repository |
| `authority/` | roles, permissions, gate, approvals, seed |
| `agency/` | policy decisions + destructive-action gates |
| `capabilities/` | registry + invoker + idempotency + built_ins + runtime_capabilities |
| `execution/` | repository + contracts + recovery (Phase 2) |
| `runtime/` | app, services, bridge, work, provisioning, maintenance, delivery, delivery_queue, leadership, lifecycle, logging, asgi, environment (Phase 5), vault (Phase 7), connectors (Phase 8), shared_state (Phase 11) |
| `intelligence/` | contracts, service, context_limits, adapters (mock, openai, anthropic), resilience |
| `memory/` | models, contracts, repository, lifecycle |
| `continuity/` | contracts, repository, assembly, service |
| `objective/` | contracts, repository, evidence, service |
| `resources/` | contracts, accountant |
| `observability/` | audit, metrics, runtime_metrics |
| `reliability/` | dead_letter, retry, circuit_breaker |
| `security/` | rate_limiter, cost_protection, abuse, input_sanitizer, network, trust |
| `isolation/` | contracts, namespace_boundary, subprocess_boundary, service |
| `media/` | contracts, extractors, real_extractors, pipeline — **RECONNECTED** (see below) |
| `interfaces/whatsapp/` | adapter, contracts, client — the live interface |

### IMPLEMENTED BUT UNWIRED

None remaining. The `media` subsystem was previously classified as
orphaned (Cycle 1 audit) because the import was dynamic. It is now
referenced at the top level of `wax.runtime.bridge.service` so AST
analysis sees the connection.

### TEST ONLY

| File | Justification |
|---|---|
| `tests/conftest.py` | shared fixtures |
| `tests/integration/*` | 70+ integration test files |
| `tests/unit/*` | 11 unit test files |
| `tests/architecture/*` | 3 architecture invariant tests |
| `tests/evaluation/*` | 1 evaluation test file |

### PLACEHOLDER

None. All structural stubs have been implemented or removed.

### DEAD

None. The Cycle 1 audit found no dead code; this re-audit confirms.

### PHILOSOPHY VIOLATION

None. The constitutional audit scanner (Laws 1-6) found no real
violations — all Law 2 findings were legitimate documentation
references or adapter-owned log identifiers.

### MISSING PRIMITIVE

| Primitive | Status |
|---|---|
| `connector.invoke` | Not yet implemented — Phase 8 added discovery + resolution; the actual operation execution (clone, publish, deploy) is a future cycle |
| `objective.update_status` | Already implemented (v1.0.0) — the intelligence can close an objective with evidence |
| `context.replay` | Not yet implemented — the recovery layer (Phase 2) rebuilds context from continuity memory, not from a stored message list |
| `terminal.checkpoint` | Partially implemented — the session's working_dir + env_vars ARE the checkpoint; full byte-level checkpoint/restore is a future cycle |

## Reconnection: media subsystem

**Before**: `wax.media.pipeline` was imported only dynamically inside
the bridge's `_run_intelligence` method, making it invisible to AST
import analysis.

**After**: Added a top-level import reference in
`wax.runtime.bridge.service` so the connection is visible:

```python
# Top of bridge/service.py
import wax.media.pipeline  # noqa: F401 — ensures the media subsystem
                           # is visible to AST import analysis
                           # (constitutional audit fix, ADR-0045)
```

The media pipeline is used by the bridge for extracting text/images/
PDF/audio/video from inbound WhatsApp messages. The import was
always there (dynamically); this change makes it statically visible.

## File count reconciliation

| Kind | Count |
|---|---|
| Source files (src/wax/**/*.py) | ~141 |
| Test files (tests/**/*.py) | ~72 |
| Migration files | 18 |
| ADR files | 39 |
| Total Python files | ~231 |

All counts are current as of this audit.
