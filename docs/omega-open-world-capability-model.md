# WAX Open-World Capability Model (OMEGA)

Verified reality at `b439aae`, plus the designed future (ADR-0026).
Companion to `docs/constitutional-audit/capability-audit.md`.

## The registry today — closed at boot, honestly

21 capabilities registered during `RuntimeServices.build()`: `echo`,
`http.get` (SSRF-guarded), `work.schedule` / `work.cancel` /
`work.list` / `work.requeue`, `message.send` (identity-derived
interface, adapter-declared delivery policy), `scratch.workspace`,
`signal.emit` (runtime-owned namespaces wait-only), `approval.list` /
`approval.cancel`, `memory.store` / `memory.search` / `memory.forget`
/ `memory.consolidate` / `memory.link`, `objective.list` /
`objective.resume` / `objective.update_status`, `code.run` (isolated),
`workspace.acquire` (integrity-pinned).

Discovery is LIVE: every turn, AVAILABLE descriptors become the
model's tool specs. Extension is NOT: no production code path registers
capabilities post-boot. That asymmetry is the honest current answer to
open-world — enforcement is complete, acquisition is not.

## The universal-primitive test

Every capability above survives it: nothing knows about education,
reminders, or any domain. A reminder is `work.schedule(kind=
"capability", payload={capability_name:"message.send", ...},
wake_at=...)` — an emergent composition (the work handler's docstring
says exactly this), not a feature.

## The gate chain — ONE component, both paths

Since the CV-12/CV-14 fix, `wax/authority/gate.py` (ApprovalGate) is
the single chain the live bridge AND the durable-work handler run:

```
registry existence/status → agency verdict (runtime policy, audited)
  → policy-approved? proceed
  → approved, unconsumed approval for THIS EXACT fingerprint?
      consume (one-time) → proceed
  → otherwise: create pending (ATOMIC via partial unique index)
      → signal ledger `approval.requested:<principal>`
      → NOTIFY the human (live channel, else durable retry state)
      → objective awaiting-human evidence sync
      → fail honestly as pending_approval
→ budget consumption → authority check (invoker) → execute under timeout
```

The model can request; the runtime decides; the human authorizes;
evidence proves. Replay impossible (one-time consumption), scope creep
impossible (fingerprint binds capability + canonical inputs), staleness
impossible (expiry swept + checked at decide time).

## Environments

Provisionable TODAY: `scratch.workspace` (TTL ≤ 24h, per-principal cap
of 5, leader-guarded reaper, audited) and `code.run` under the
namespace sandbox (user-namespace, no network, read-only root, masked
proc/sys, rlimits, process-group kill) with loud fallback to
subprocess and the executed grade reported in the result. OCR/PDF
extractors are real, tested, and self-gating but NOT wired into the
live media path yet (Phase T). Container / microVM / browser isolation
kinds are declared enums that raise "not yet implemented" — no fake
security.

## Revocation / expiry (what actually works)

Approvals expire and consume exactly once; permission removal takes
effect at next check (including work-wake); workspaces + artifacts die
at TTL; memories expire; roles are DB rows read fresh every check.
Capability-level `set_status`/`unregister` exist with no production
caller — recorded honestly in ADR-0026 as part of the future lifecycle
work.

## The open-world future

ADR-0026 holds the designed lifecycle (discover → inspect → validate →
authorize → acquire → execute → revoke → audit), the verified contract
gaps, and the ranked build order. The registry stays closed until the
gates exist; openness without provenance would be a security lie
(mission §60, §75).
