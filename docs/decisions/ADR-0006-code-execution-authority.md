# ADR-0006: Code Execution Behind Explicit Authority (Subprocess Boundary)

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX implementation agent, per roadmap Phase U

## Context

The codebase contained a `SubprocessBoundary` — sandboxed code execution
with timeouts and output caps — reachable only from tests. The audit
flagged it as TEST-ONLY. Two failure modes were possible: leave it
unwired (runtime cannot execute anything) or wire it carelessly (AI code
runs unsandboxed with runtime privileges).

## Decision

Code execution is exposed as a **capability** (`code.run`) that composes
three independent controls:

1. **Isolation** — execution always inside the subprocess boundary
   (fresh interpreter, no runtime imports, wall-clock timeout, output
   cap). The boundary is not bypassable through the capability surface.
2. **Authority** — every invocation requires an explicit authority grant
   for `code.run`. The AI principal has no inherent right to execute
   code; permission is per-principal, per-capability, revocable.
3. **Accounting** — execution time and output size are metered by the
   Resource Accountant and attributed to the invoking principal.

Agency (human approval) composes on top: a principal may be required to
obtain approval before specific executions per policy.

## Consequences

- The TEST-ONLY boundary is now a production path with three independent
  safety layers.
- A compromise of the intelligence layer still cannot escalate: the
  capability surface is the only execution route, and it enforces
  isolation + authority + accounting.
- Failure returns the real error (timeout, nonzero exit, output cap) —
  never a fabricated success.
