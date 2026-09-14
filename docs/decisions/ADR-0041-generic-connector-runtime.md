# ADR-0041: Generic Connector Runtime

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 8 — Generic Connector Runtime (OMEGA Implementation Directive)

## Context

Phase 7 added the credential vault with connector definitions
(`git_host`, `package_registry`, `cloud_deployment`, `file_storage`,
`messaging`). The vault stores secrets and issues opaque handles.
But the intelligence has no way to:

- Discover what connectors are available (query the connector
  definitions)
- Resolve a grant handle to a service binding it can actually use
- Invoke connector operations (clone a git repo, publish a package,
  deploy to cloud) through the SAME gated path as other capabilities

The directive (Phase 8) requires resource-type-based connector
discovery. The runtime understands resource types, NOT brands. The
intelligence discovers actual services (GitHub, GitLab, Codeberg,
npm, PyPI, Railway, Fly.io, Google Drive, Dropbox) through the
environment — no architectural change when a new platform appears.

## Decision

Add two capabilities built on Phase 7's vault:

### `connector.discover` (v1.0.0)

Input: none (or optional `connector` filter)
Output: list of available connector definitions with their supported
scopes + auth methods.

The intelligence uses this to ask "what can I do?" — e.g. "I need
to clone a repo; is `git_host` available? What scopes does it support?"

### `connector.resolve` (v1.0.0)

Input: `grant_handle` (from `credential.request`), `connector` (resource type)
Output: `binding_handle` (opaque), `service_kind` (the discovered
brand, e.g. "github" or "gitlab"), `available_operations` (list)

The runtime resolves the grant to a service binding. The
`service_kind` is the discovered brand — the intelligence learns it
through the environment, NOT through hardcoded architecture. A
future cycle will add `connector.invoke` that uses the binding handle
to execute operations (clone, push, publish, deploy).

### Architecture

The connector runtime lives in `wax.runtime.connectors`. It queries
Phase 7's `connector_definitions` table + the principal's connections
to answer discovery + resolution queries.

The intelligence NEVER sees:
- raw secrets (only handles)
- host paths (only opaque IDs)
- the vault's encryption key

The intelligence DOES see:
- connector resource types (git_host, etc.)
- supported scopes (repository.read, etc.)
- the discovered service kind (github, gitlab — learned through the
  environment, not hardcoded)

## Alternatives considered

### Alternative 1: Hardcode GitHub/GitLab/etc. as separate capabilities

Rejected — that's exactly the "brand coupling" Law 2 forbids. A new
platform would require code changes.

### Alternative 2: A single `connector.invoke` capability with brand parameter

Considered — but the brand should be DISCOVERED, not passed as a
parameter. The resolution step (grant → service binding) is where
the brand is learned.

## Consequences

### Positive

- The intelligence can discover available connectors + scopes
  without hardcoding.
- New platforms absorb by adding a connector_definition row, not by
  changing code.
- The resolution step teaches the intelligence the service kind
  (github, gitlab) through the environment.

### Negative

- `connector.invoke` (the actual operation execution) is deferred to
  a future cycle — Phase 8 only adds discovery + resolution.
- The service kind discovery is mocked for now (the runtime returns
  "github" for `git_host` connections); a future cycle will add
  real brand detection via the secret's structure or a config map.

## Tests

`tests/integration/test_connector_runtime.py` covers:

- connector.discover lists all universal connectors
- connector.discover with a filter returns only matching connectors
- connector.resolve returns a binding handle + service kind
- connector.resolve rejects invalid grant handle
- connector.resolve rejects wrong principal
- the intelligence never sees raw secrets
