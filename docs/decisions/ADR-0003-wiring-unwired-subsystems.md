# ADR-0003: Wiring the Unwired — RuntimeServices as the Single Live Path

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX implementation agent, per forensic audit mandate

## Context

The forensic audit found the codebase contained substantially complete
subsystems — Capability Registry, Capability Invoker, Authority, Agency,
Resource Accountant, Security, Metrics, Continuity, Reliability — that were
**never instantiated in the live request path**. They existed as imports-only
or test-only code. The production path (webhook → bridge → intelligence →
reply) bypassed every one of them.

This is the audit's central finding: the architecture existed but was not the
operating system it claimed to be. Nothing enforced Authority; nothing was
accounted; nothing was observed.

## Decision

`RuntimeServices.build()` is the **only** construction point for runtime
subsystems, and it is invoked inside the application lifespan
(`create_app()`). The bridge receives the assembled container and routes
every message through:

1. **Identity resolution** (interface-independent principal)
2. **Security enforcement** (rate limit, abuse heuristics, cost guard)
3. **Continuity** (interrupted-conversation resume before new intake)
4. **Intelligence** (provider-agnostic, tool-calling contract)
5. **Capability invocation** (Agency gate → Authority check → Resource
   accounting → Invoker → audit log)
6. **Observability** (metrics emitted at each boundary)

No subsystem may be constructed outside the container. Architecture tests
forbid direct construction in the bridge layer.

## Consequences

- Every production message now passes through enforcement that previously
  existed only in tests.
- Deleting or bypassing `RuntimeServices` is detectable by the architecture
  test suite (INV-04, INV-06 enforced).
- The live path is auditable end-to-end: every capability invocation carries
  principal, authority verdict, and resource cost.
