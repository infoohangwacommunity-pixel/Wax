# ADR-0045: Constitutional Cleanup

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 12 — Constitutional Cleanup (OMEGA Implementation Directive)

## Context

Per Article 6 (Every Component Must Justify Its Existence) and Article
25 (The Constitutional Audit), every file must be classified honestly
as LIVE / IMPLEMENTED BUT UNWIRED / TEST ONLY / PLACEHOLDER / DEAD /
PHILOSOPHY VIOLATION / MISSING PRIMITIVE.

The Cycle 1 audit identified the `media` subsystem as an orphan (no
other subsystem imports it at the AST level). This phase verifies
the current state and reconnects or documents honestly.

## Decision

### 1. Reconnect the media subsystem

The `media` subsystem's pipeline (`wax.media.pipeline`) IS imported
by the bridge's context assembly — but only for extracting media from
inbound WhatsApp messages. The AST-level orphan finding was because
the import is dynamic (inside a function), not at module top-level.

Fix: add a top-level import in `wax.runtime.bridge.service` that
references the media pipeline so AST analysis sees the connection.

### 2. Classify every subsystem

Walk every `src/wax/` directory and classify:
- LIVE: imported by runtime code in production paths
- IMPLEMENTED BUT UNWIRED: code exists, runtime doesn't use it
- TEST ONLY: only serves tests
- PLACEHOLDER: structural stub
- DEAD: no meaningful runtime purpose
- PHILOSOPHY VIOLATION: works but breaks constitutional law
- MISSING PRIMITIVE: a capability should emerge here but infrastructure is incomplete

### 3. Remove or document dead code

For any DEAD classification: remove with documented justification.
For IMPLEMENTED BUT UNWIRED: reconnect or document honestly.

## Tests

The audit results are persisted in `docs/constitutional-audit/phase-12-cleanup.md`.
No new test files — this phase is documentation + reconnection.
