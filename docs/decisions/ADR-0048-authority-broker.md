# ADR-0048: Authority Broker — Secure Out-of-Band Authority Intake

**Status**: Accepted
**Date**: 2026-09-15
**Cycle**: P0-Authority (secure control plane + human handoff)

## Context

The repository review identified a critical security gap: the
`credential.connect` capability accepts a raw `secret` in the
model-facing tool call. This means a secret can enter:

- model tool-call arguments
- execution_steps.inputs
- audit_events.payload
- memory
- work payloads
- logs

The handoff document requires: "Secrets are entered only through a
secure control plane, encrypted immediately, and used internally
through opaque authority references. They never pass through
WhatsApp, model prompts, model tool arguments, ordinary logs,
memory, artifacts, URLs, or execution evidence."

## Decision

Build an **authority broker** that is the SOLE path through which
the intelligence requests external authority. The model NEVER
supplies a raw secret.

### Flow

1. Intelligence calls `authority.request` with purpose + requested_actions
   (NO secret in the input)
2. Runtime creates a `human_handoff` record (pending → awaiting_human)
3. User receives a notification (via WhatsApp or the runtime signal)
4. User opens the WAX control plane (secure dashboard)
5. User pastes the secret into the secure form (password input,
   no autocomplete, no caching, no echo)
6. Backend encrypts the secret IMMEDIATELY (AES-GCM, per ADR-0047)
7. Runtime creates an `authority_material` record (encrypted)
8. Runtime creates an opaque `authority_grant` (handle-based)
9. Runtime emits `authority.handoff_completed` signal
10. Waiting work resumes with a typed observation
11. Intelligence uses the grant handle (never the secret)

### What the model sees

The model receives ONLY:
- `handoff_ref` (opaque ID)
- `status` (awaiting_human / active / failed)
- `authority_ref` (opaque handle, after completion)
- `expires_at`

The model NEVER sees:
- the raw secret
- the ciphertext
- the encryption key
- the nonce
- any decryption output

### Data model

Three new tables:
- `human_handoffs`: the request for human action (challenge, status, evidence)
- `authority_materials`: the encrypted secret (AES-GCM envelope)
- `authority_grants`: opaque permission to use authority (bound to principal + objective + execution + environment)

### Capabilities

- `authority.request` — the intelligence requests authority (no secret)
- `authority.status` — check the status of a handoff
- `authority.revoke` — revoke an authority grant

### Control plane

The control plane is a server-rendered same-origin dashboard with:
- `GET /control/handoffs/{id}` — view the handoff (metadata only)
- `POST /control/handoffs/{id}/submit` — submit the secret (encrypted immediately)

The dashboard:
- Requires authentication (challenge-bound to principal_id)
- Uses HttpOnly, Secure, SameSite cookies
- Has CSRF protection
- Sets Cache-Control: no-store
- Never echoes the secret after submission
- Never displays the secret value

## Design Principle

> The runtime provides affordances, not workflows.
> The intelligence decides WHEN authority is needed.
> A human handoff is an available mechanism, not a mandatory step.
> Authority is requested only when the discovered path requires it.
