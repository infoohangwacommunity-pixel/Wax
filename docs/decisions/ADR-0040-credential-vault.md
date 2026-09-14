# ADR-0040: Credential Vault

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: Phase 7 — Credential Vault (OMEGA Implementation Directive)

## Context

Per Law 4 (Secrets Never Enter Intelligence), credentials must NEVER
enter prompts, memory, logs, artifacts, commits, or ordinary chat.
Today WAX has interface credentials (`PrincipalCredential` for
WhatsApp phone numbers) but no generic vault for external service
credentials (git hosts, package registries, cloud deployment platforms,
file storage, messaging platforms).

The directive (Phase 7) requires a provider-neutral vault supporting:
- registration (the principal authenticates a service)
- secure storage (encrypted at rest)
- scoped grants (objective + execution bound, expiring)
- temporary injection (into an approved environment boundary, never
  into the model's prompt)
- rotation (a new secret replaces the old; old is revoked)
- revocation (a connection or grant is revoked)
- expiration (TTL-based)
- audit (every credential event is recorded)
- provider-neutral (the vault understands resource types, not brands)

## Decision

### Resource types, not brands

The vault's `ConnectorDefinition` declares a resource type:
- `git_host` (GitHub, GitLab, Codeberg — discovered by the intelligence)
- `package_registry` (npm, PyPI — discovered)
- `cloud_deployment` (Railway, Fly.io — discovered)
- `file_storage` (Google Drive, Dropbox — discovered)
- `messaging` (WhatsApp, future adapters — discovered)

The vault NEVER hardcodes a brand. Connector definitions are
declarative; the runtime resolves them to actual services at injection
time.

### Persistence (migration `e4d6f8a0b2c6`)

Four new tables:

- `connector_definitions`: declared resource types (stable name,
  version, supported scopes, authentication methods). Seeded at
  startup with the universal set.
- `principal_connections`: a principal has connected a service
  (connector, status, granted_scopes, consent_time, expiry,
  revocation state, last-used time).
- `credential_grants`: a temporary objective/execution-scoped
  authorization (principal, objective, execution, connector, scopes,
  purpose, expiry, revocation, audit linkage).
- `credential_events`: append-only audit log (connected, verified,
  injected, used, rotated, revoked, expired, denied).

### Secret storage

Secrets are stored ENCRYPTED at rest in the `principal_connections`
table's `secret_blob` column. The encryption key is read from
`WAX_VAULT_KEY` environment variable (the runtime never logs this
key; it is injected into the vault service at startup).

If `WAX_VAULT_KEY` is not set, the vault uses a development-mode
XOR cipher with a hardcoded key (loudly logged as a warning —
production deployments MUST set `WAX_VAULT_KEY`).

### Capabilities (4 new)

- `credential.connect` (v1.0.0): the principal registers a credential
  for a connector. The runtime validates the connector definition,
  encrypts the secret, records the connection + consent. Returns a
  `connection_id` (NOT the secret).
- `credential.request` (v1.0.0): the intelligence requests a scoped
  grant for an objective/execution. The runtime validates the
  connection exists + has the requested scopes, creates a grant with
  TTL, returns a `grant_id` + `handle` (opaque). The intelligence
  passes the handle to the environment lease (Phase 5); the vault
  injects the actual secret into the environment boundary at
  provisioning time — never into the model's prompt.
- `credential.list` (v1.0.0): list the principal's connections
  (metadata only; never the secret).
- `credential.revoke` (v1.0.0): revoke a connection or grant. The
  revocation is immediate; the secret is purged from any in-flight
  injection.

### Architecture boundary

The vault lives in `wax.runtime.vault`. The intelligence sees only
the 4 capabilities + the opaque handles. The vault NEVER exposes the
secret to:
- the model (prompt, tool arguments)
- memory
- logs
- artifacts
- commits

The vault's `inject_into_environment(environment_id, grant_id)` method
is called by the environment planner (Phase 5) when provisioning an
environment with a credential requirement. The secret is injected
into the subprocess environment of the terminal session (Phase 6) —
the model never sees it.

## Alternatives considered

### Alternative 1: Store secrets in environment variables

Rejected — environment variables are visible to the model if the
model can read `os.environ`. The vault's `inject_into_environment`
filters out secret env vars from any model-visible context.

### Alternative 2: Use an external secret manager (AWS Secrets Manager, etc.)

Considered — but the directive requires provider neutrality. The
vault's storage backend is pluggable: today it's encrypted-at-rest in
the DB; a future deployment can plug in an external secret manager
without changing the capability contracts.

## Consequences

### Positive

- Secrets NEVER enter intelligence — the vault's contract is that
  the model sees only handles, never raw values.
- Scoped grants expire — a stolen handle is useless after the TTL.
- Rotation invalidates old handles — a compromised secret can be
  rotated without revoking the connection.
- The vault is provider-neutral — adding a new connector type is a
  declarative act, not a code change.

### Negative

- One new migration (4 tables).
- The encryption key MUST be set in production; the development-mode
  XOR cipher is loud-logged as a warning.

## Security

- The vault NEVER logs secret values. Audit events record the
  connection_id + grant_id + scopes, never the secret.
- The vault's `inject_into_environment` filters secret env vars
  from any model-visible context (the `os.environ` of the subprocess
  is set, but the model's `os.environ` is not).
- The vault's encryption key is read from `WAX_VAULT_KEY` at startup;
  it is never serialized, never logged, never committed.
- Grant handles are ULIDs — opaque, unguessable, scoped to a single
  objective/execution.

## Tests

`tests/integration/test_credential_vault.py` covers:

- credential.connect registers a connection (returns connection_id,
  not the secret)
- credential.list returns metadata only (no secrets)
- credential.request creates a scoped grant with TTL
- credential.revoke immediately invalidates
- grant expiration
- secret storage is encrypted at rest (the DB row's secret_blob is
  not the plaintext)
- the vault NEVER exposes secrets via the capability API
- wrong principal cannot use a connection
- nonexistent connector is rejected
- insufficient scopes are rejected
