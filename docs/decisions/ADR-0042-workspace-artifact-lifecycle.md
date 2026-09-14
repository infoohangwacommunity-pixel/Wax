# ADR-0042: Workspace + Artifact Lifecycle

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 9 — Workspace and Artifact Lifecycle (OMEGA Implementation Directive)

## Context

WAX has:
- `workspace.acquire` (ADR-0023): downloads artifacts FROM external sources INTO a workspace, with SHA-256 verification + content-addressed caching + host allowlist.
- `ArtifactRecord` (ADR-0023): persistence for artifacts (owner, objective, execution, hash, size, lifecycle).
- `ProvisioningService`: scratch workspaces with TTL.

What's MISSING per the directive (Phase 9):
- Workspace snapshots (save/restore state)
- Artifact capture FROM a workspace (a file produced by `terminal.execute` becomes a durable artifact)
- Artifact integrity verification (SHA-256 re-verification on retrieval)
- Artifact retrieval/export/delivery (the intelligence can ask for an artifact's metadata; bytes go through the delivery path)

## Decision

Add 5 capabilities:

### `workspace.snapshot` (v1.0.0)

Input: `workspace_id` (provisioned resource ID)
Output: `snapshot_id`, `file_count`, `total_bytes`, `files` (list of {path, sha256, size})

Captures the current state of a workspace: walks the directory, hashes each file, persists a `WorkspaceSnapshotRecord`. The snapshot is content-addressed (same files → same snapshot ID, idempotent).

### `workspace.restore` (v1.0.0)

Input: `snapshot_id`, `target_workspace_id`
Output: `restored_files`, `total_bytes`

Restores a snapshot into a target workspace. The target must be an active provisioned workspace owned by the principal. Files are written atomically (temp file + rename).

### `workspace.promote` (v1.0.0)

Input: `workspace_id`
Output: `promoted`, `permanent_resource_id`

Promotes a TTL-bound workspace to permanent (clears `expires_at`). The workspace survives the reaper. Use for workspaces that hold durable artifacts.

### `artifact.capture` (v1.0.0)

Input: `workspace_id`, `path` (workspace-relative), `filename`, `objective_id` (optional)
Output: `artifact_id`, `sha256`, `size_bytes`

Captures a file from a workspace as a durable `ArtifactRecord`. Computes SHA-256, records provenance (which workspace + execution produced it). The file stays in the workspace; the artifact record is the queryable evidence.

### `artifact.list` (v1.0.0)

Input: optional `objective_id` filter
Output: `artifacts` (list of {id, filename, sha256, size, source, objective_id, created_at})

Lists the principal's artifacts. Metadata only — NEVER file bytes.

### `artifact.retrieve` (v1.0.0)

Input: `artifact_id`
Output: `artifact` (metadata) + `integrity_verified` (re-hashes the file and compares)

Retrieves an artifact's metadata. Re-verifies integrity by re-hashing the file (if it still exists in the workspace). If the file is gone (workspace expired), returns `integrity_verified: false` with an honest note.

## Persistence

A new `workspace_snapshots` table:
- `id`, `principal_id`, `workspace_resource_id`, `files_json` (JSON list of {path, sha256, size}), `total_bytes`, `created_at`

The existing `artifacts` table (ADR-0023) is reused; Phase 9 adds the capture capability that populates it.

## Alternatives considered

### Alternative 1: Store artifact bytes in the DB

Rejected — the DB is for metadata; bytes live in the workspace filesystem. The artifact record points to the workspace + path; the bytes are read through the workspace boundary.

### Alternative 2: No snapshots — rely on workspace TTL

Rejected — the directive explicitly requires snapshots + restore. A snapshot is the resumability primitive for workspaces (analogous to execution checkpoints from Phase 2).

## Tests

`tests/integration/test_workspace_artifact_lifecycle.py` covers:
- workspace.snapshot captures the current state
- workspace.restore restores into a target workspace
- workspace.promote clears the TTL
- artifact.capture records a file as a durable artifact
- artifact.list returns metadata only
- artifact.retrieve re-verifies integrity
- wrong principal rejected
