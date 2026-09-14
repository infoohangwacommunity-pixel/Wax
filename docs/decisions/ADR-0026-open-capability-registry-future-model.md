# ADR-0026: Open Capability Registry — The Safe Future Model, Designed Not Built

Date: 2026-09-14
Status: Accepted (design recorded; implementation deferred until evidence justifies it)
Context: OMEGA mission §32–§40 (open-world capability model), §60 (open capability registry decision).

## Context

The capability audit verified the registry's current reality at
`10c2333`:

- **Closed at boot, honestly.** 21 capabilities are registered during
  `RuntimeServices.build()` (2 built-ins + 19 runtime capabilities).
  The registry is an in-memory dict; nothing in production calls
  `register`/`unregister`/`set_status` after boot; no plugin loader,
  entry-point, or dynamic import machinery exists anywhere. Discovery
  is exposed (the model sees every AVAILABLE descriptor as a tool spec
  each turn); extension is not.
- **The enforcement half of an open registry already exists and is
  strong:** DB-backed role→permission authorization with wildcard
  matching and audit on every decision; the agency gate + human-approval
  primitive (fingerprint-bound, expiring, exactly-once consumption —
  hardened this cycle); per-execution resource budgets; rate/cost/abuse
  perimeter; the invoker as the sole effect point with declared-input
  validation and timeout; namespace/subprocess isolation with loud
  degradation; integrity-pinned artifact acquisition (mandatory
  SHA-256, host allowlist, content-addressed cache, never executed as
  code); full audit trail.
- **The registry half is what's missing.** Verified contract gaps:
  `output_schema` is declared on 11/21 capabilities and validated on
  none; `CapabilityInvocationRequest.idempotency_key` is accepted and
  never read; descriptor `version` is write-only (logged once);
  `REGISTERED/DEGRADED/UNAVAILABLE/REVOKED` statuses have no production
  writer; no provenance/origin/signature field; no per-capability
  resource envelope; no persistent (DB-backed) registry.

§60 orders: "Do not immediately make it open. Design the safe future
model … Only implement what is justified by evidence and available
infrastructure. Do not pretend third-party capability acquisition is
secure if it is not."

## Decision

**1. The registry stays closed at boot.** A closed registry with real
enforcement is honest; an open registry without provenance, signing,
and code-binding infrastructure would be a security lie. No dynamic
registration is implemented in this cycle.

**2. The safe future model is defined as a lifecycle with hard gates.
Every stage must exist and be enforceable BEFORE the next is wired:**

```
discover → inspect → validate → authorize → acquire → execute → revoke → audit
```

| Stage | Gate that must hold | Exists today |
|---|---|---|
| discover | capability encountered via an interface, artifact, or external service — never via prompt text alone | partial (tool-surfacing only) |
| inspect | machine-readable contract: identity, version, inputs, outputs, permissions, side-effect class, resource envelope, security constraints, ownership, lifecycle, provenance | partial (schemas + permission + destructive flag only) |
| validate | schema validation of inputs AND outputs; duplicate/namespace checks; dependency + isolation-grade declaration | partial (inputs only) |
| authorize | human-approved REGISTRATION (not invocation) — a registration is an authority event with its own approval, provenance review, and quarantine | missing |
| acquire | integrity-pinned artifact acquisition + signature/attestation + dependency isolation + credential isolation (model never receives raw credentials) | partial (acquire-as-data exists; verify→wrap→register missing) |
| execute | invoker + isolation + budgets, unchanged | exists |
| revoke | wired status transitions with propagation; grant TTL on role assignments; temporary capability cleanup | partial (permission removal is immediate; capability-level revoke unwired) |
| audit | every stage recorded as operational evidence | exists |

**3. The missing primitives, ranked (the build order when evidence
justifies):**
1. Persistent (DB-backed) descriptor storage with status lifecycle —
   multi-instance consistency requires shared registry state.
2. Descriptor provenance & security metadata: origin, publisher,
   signature/hash, side-effect class, egress scope, secret-exposure
   flag, required isolation grade, resource envelope.
3. Output-schema validation in the invoker (closes the declared-but-
   unenforced output contract).
4. Honesty cleanup on the request contract: honor or remove
   `idempotency_key`; wire or document the `idempotent` flag.
5. Registration-approval flow (the approval primitive generalized from
   invocations to registrations) + quarantine.
6. Verify→wrap→register pipeline turning a signed artifact into a
   capability under a per-capability sandbox profile.
7. Revocation propagation + grant TTLs.
8. Environment negotiation protocol (§37): machine-readable
   `environment_requirements` on descriptors; the model requests a
   capability/environment; the runtime answers what is available or
   safely provisionable. Isolation grade remains a RUNTIME decision
   (code_run.py is explicit: never the model's) — negotiation proposes,
   the runtime disposes.

**4. Non-negotiables carried forward regardless of openness:** the model
never receives raw credentials; discovery never equals trust; capability
descriptions are untrusted content (injection defense per §48); a
revoked or expired capability must fail loudly at invocation AND at
work-wake time; security always wins over openness.

## Consequences

- The runtime can honestly answer §82's acceptance question 10 today:
  an unknown capability request fails with a structured, honest
  "no such capability" — nothing is fabricated, nothing is acquirable
  yet, and the answer says exactly that.
- When a real user arrives with a real need the registry cannot serve,
  this ADR is the build order — no design will need to be invented
  under pressure.
