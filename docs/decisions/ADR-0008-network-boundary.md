# ADR-0008: Network Boundary — Capability Egress Never Trusts URLs

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX principal-engineer continuation session

## Context

`http.get` fetched any URL the AI supplied. A capability principal with
`capability.invoke:built_in` could therefore reach:

- `http://127.0.0.1:*` — the runtime's own processes
- `http://169.254.169.254/latest/meta-data/` — cloud instance credentials
- RFC1918/CGNAT/link-local LAN space — databases, sidecars, admin panels
- non-HTTP schemes and credential-bearing URLs

The Foundation PDF (§16) is explicit: "network access" is a runtime
enforcement boundary. The AI may request the fetch; whether the network
is reachable is the runtime's decision. (Hostile-content fetches are also
an injection surface — §16 names "malicious external content".)

## Decision

`wax.security.network` is the single mechanism every outbound capability
fetch composes:

1. **Scheme allowlist** — http/https only; credentials in URLs rejected.
2. **Address-truth validation** — the runtime resolves DNS itself and
   classifies the resolved addresses (`ipaddress.is_global`), so literal
   tricks (2130706433, 0x7f000001, ::ffff:127.0.0.1) and DNS-rebinding
   shapes are judged on the ADDRESS, not the string. Only global unicast
   passes.
3. **Redirects re-validated per hop** — a public URL that 302s into
   private space is blocked.
4. **Streamed byte cap** — a hostile server cannot OOM the runtime.
5. **Caller-safe errors** — the AI learns the constraint ("blocked by
   network boundary: …"), never internal topology.

## Consequences

- Capabilities compose the boundary; none re-implement it. A future
  `http.post` or webhook-ping capability inherits the guarantee.
- The AI experiences a truthful constraint, consistent with "never fake
  success": the block message states what is not allowed and why.
- Defence-in-depth note: this complements Authority (who may invoke
  http.get at all), it does not replace it.
