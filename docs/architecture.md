# WAX Architecture

## Trust model

The intelligence operates with broad terminal access:
- Full network access (no host allowlist, no SSRF guard)
- Full filesystem access (no path containment)
- Inherited environment variables (LLM keys, DB URLs, API keys)
- No model-facing capability catalogue
- No AI authority/approval broker

Operational process limits (timeout, output truncation) are execution
plumbing, not action authorization.

## The terminal

The terminal (`src/wax/runtime/executor.py`) is the universal environment
interface. It is NOT a capability. It is NOT a registered tool. It is
infrastructure.

The model sees one tool: `terminal`. Parameters:
- `command` (str) — the shell command to execute
- `mode` (foreground | detached) — wait for result or start background
- `timeout` (float, optional) — foreground process timeout

The terminal inherits the full process environment. The working directory
persists across terminal calls within the same execution. Detached
processes survive after the foreground command returns (for servers,
workers, tunnels).

## The bridge loop

```
message → identity → idempotency → execution
        → memory retrieval (automatic)
        → context assembly
        → model ↔ terminal (loop until done)
        → memory extraction (automatic)
        → delivery
```

The bridge (`src/wax/runtime/bridge/service.py`) is ~450 LOC, down from
1,745 in the old architecture. It no longer constructs capability tool
catalogues, routes through authority gates, or tracks resource budgets.

## Memory

Memory is a runtime subsystem, NOT a model-facing tool. The runtime:
- Retrieves relevant memories BEFORE the model runs (automatic)
- Extracts memories AFTER the interaction (automatic)
- Detects contradictions and supersedes old memories
- Forgets when explicitly requested

The model does not call `memory.search` or `memory.store`. The runtime
handles memory formation automatically.

## Durable work

Work survives restarts. The work runner claims due items, wakes the
intelligence with a reentry context, and the intelligence continues.
This is the mechanism for "check this again tomorrow" or "continue when
the user replies."

## Database

Surviving tables (12):
- principals, principal_credentials — identity
- conversations — conversation threading
- memory_records, memory_links — memory
- executions, execution_steps — execution history
- work_items, runtime_signals — durable work
- processed_messages — idempotency
- delivery_records — outbound delivery
- audit_events — append-only operational ledger

Removed tables (post-reset): roles, principal_roles, pending_approvals,
capability_invocations, artifacts, workspace_snapshots,
provisioned_resources, objectives, objective_executions,
dead_letter_entries, and others.
