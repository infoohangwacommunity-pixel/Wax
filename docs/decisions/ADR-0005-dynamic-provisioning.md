# ADR-0005: Dynamic Provisioning — Ephemeral Resources with Owners and Lifecycles

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX implementation agent, per roadmap Phase S

## Context

An open-world runtime must let an AI request scratch space — a workspace, a
temporary file, a throwaway context — without the runtime hardcoding what
it is for. The audit found no provisioning mechanism: either resources were
global, or they did not exist.

## Decision

Provisioned resources are first-class runtime objects with **mandatory**
properties:

- **Owner** (principal that requested it; authority checked at request time)
- **Scope** (private to owner, or explicitly shared)
- **TTL** (every ephemeral resource expires; expiry is enforced by the
  runtime sweeper, not by the AI remembering to clean up)
- **Limits** (size ceiling, count ceiling per owner — enforced at
  allocation, not after the fact)
- **Audit trail** (allocate, access, expire, destroy are all recorded)
- **Cleanup** (destruction is complete: contents are not merely unlinked
  but unrecoverable through the runtime)

The runtime never fabricates success: a request that exceeds limits returns
the real constraint to the AI, which may adapt its plan.

## Consequences

- Ephemeral resources cannot leak: TTL + sweeper guarantee reclamation.
- The AI cannot exceed its authority by side effect: every allocation is
  authority-checked like any capability invocation.
- No domain semantics are embedded: a "study notes scratchpad" and a
  "data-analysis workspace" are the same mechanism with different
  parameters chosen by the AI.
