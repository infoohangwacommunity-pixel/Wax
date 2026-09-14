# ADR-0039: Terminal Runtime

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 6 — Terminal Runtime (OMEGA Implementation Directive)

## Context

WAX has `code.run` (one-shot Python execution in a namespace sandbox) and
Phase 5's `environment.request` (provisions a bounded environment with
workspace + isolation + network). But the intelligence has no way to:

- Open a PERSISTENT terminal session that survives across multiple commands
- Run shell commands (not just Python) in a governed environment
- Manage process groups (kill child processes on session close)
- Checkpoint session state (working directory, env vars) for resume
- Recover a crashed terminal session

The directive (Phase 6) requires all of these.

## Decision

Add three capabilities built on Phase 5's environment negotiation:

### `terminal.session.open` — open a persistent session

Input: `environment_id` (from `environment.request`) + optional `working_dir`
Output: `session_id`, `state` (active), `expires_at`

The session is BOUND to the environment lease. When the environment is
released, the session is closed. The session has its own TTL (≤ the
environment's TTL).

### `terminal.execute` — run a command in a session

Input: `session_id`, `command` (the shell command), `timeout_seconds`
Output: `exit_code`, `stdout` (bounded), `stderr` (bounded), `timed_out`,
`duration_ms`, `truncated`

The command runs in the session's environment (workspace + isolation +
network policy). The session's working directory and env vars persist
across commands.

### `terminal.session.close` — close a session

Input: `session_id`
Output: `closed`, `state`

Closes the session, kills any child process group, releases the
environment binding. The session record stays for audit.

### Persistence: `terminal_sessions` table

- `id`, `principal_id`, `execution_id`, `environment_id` (FK to
  environment_leases.id, CASCADE on release)
- `status` (active/closed/expired/failed)
- `working_dir` (workspace-relative path, never absolute host path)
- `env_vars` (JSON — session env vars the intelligence set)
- `expires_at` (TTL)
- `last_command_at` (for idle-session reaping)
- `last_exit_code` (last command's exit code)

### Process group governance

The terminal execute spawns the command in a process group. On session
close, the entire group is killed (SIGTERM → SIGKILL after a grace
period). No orphan processes survive session close.

### Resource governance

The session inherits the environment's resource limits:
- `cpu_seconds` — total CPU budget across all commands
- `memory_bytes` — per-command RSS limit
- `timeout_seconds` — per-command wall-clock limit
- The isolation boundary enforces these via rlimits/cgroups.

### Checkpoints + recovery

The session's `working_dir` + `env_vars` ARE the checkpoint. On crash
recovery (Phase 2's `recover_execution`), a session in `active` state
with no recent `last_command_at` is marked `expired` honestly. The
intelligence can re-open a new session with the same working_dir +
env_vars if it needs to resume.

### Architecture boundary

The terminal capabilities live in `wax.capabilities.runtime_capabilities`
(the same place as all other runtime capabilities). The session
persistence lives in `wax.state.terminal_models`. The terminal executor
wraps the existing `wax.isolation` boundary — it does NOT bypass it.

## Alternatives considered

### Alternative 1: Extend `code.run` to accept multiple commands

Rejected — `code.run` is a one-shot capability by design. Persistent
sessions have different lifecycle semantics (TTL, process groups,
working directory state). Mixing them would muddy both contracts.

### Alternative 2: A separate `TerminalService` class

Considered — but the existing pattern is that capabilities close over
`RuntimeServices`. Adding a separate service would create a new seam.
The terminal capabilities close over `services` like all others.

## Consequences

### Positive

- The intelligence can run shell commands in a governed, persistent
  environment — the universal primitive for software-building objectives.
- Process group governance prevents orphan processes.
- Session state (working_dir, env_vars) is checkpointed per command,
  enabling resume after crash.
- The terminal respects the environment's network policy + isolation
  grade — no bypass.

### Negative

- One new migration (terminal_sessions table).
- The terminal executor needs to enforce process groups, which is
  platform-specific (Unix only today; Windows support is future).

## Security

- The command runs in the session's environment isolation boundary —
  the SAME boundary `code.run` uses. No bypass.
- The command is logged in the execution step record (inputs + outputs)
  for audit. stdout/stderr are bounded (default 64KB each) to prevent
  log explosion.
- The working_dir is workspace-relative; the intelligence never sees
  the absolute host path.
- env_vars are validated: no secret-like patterns, no PATH overrides
  that could escape the workspace.

## Tests

`tests/integration/test_terminal_runtime.py` covers:

- terminal.session.open creates an active session
- terminal.execute runs a command and returns exit_code + stdout
- terminal.execute persists working_dir across commands
- terminal.session.close kills the process group
- session TTL expiry
- command timeout enforcement
- stdout/stderr truncation at the bound
- env var validation (no secrets)
- workspace-relative path enforcement (no absolute paths)
