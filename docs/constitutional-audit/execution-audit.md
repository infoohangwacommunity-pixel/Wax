# Execution Audit — Constitutional Audit

Scope: NamespaceBoundary, SubprocessBoundary, workspace.acquire, code.run, approval, signals, deliveries, heartbeats, leases, fencing, recovery — one coherent model?

## The single effect point (verified)

`CapabilityInvoker` is the SOLE path from AI intent to effect, reachable from BOTH live (bridge tool loop) and durable (work runner handler) contexts through the same chain: agency gate → approval fingerprint/consume → budget → authority (`required_permission` check) → declared-contract validation (CV-5 fix) → execution with timeout → audit/step record. The two call sites duplicate this chain (drift risk → CV-12) but its LOGIC is identical, and the invoker itself is shared.

## Mechanism-by-mechanism

| Mechanism | Verdict | Evidence |
|---|---|---|
| NamespaceBoundary (isolation) | Kernel-grade, honest | Unshare flags per namespace; loud subprocess fallback with metric when unavailable (`service.py:80-96`); never silently weaker |
| SubprocessBoundary | Resource-limited | rlimits, timeout, output caps, scrubbed env, no shell |
| workspace.acquire | Integrity-pinned | network boundary (DNS resolution + IP classification + per-hop redirect revalidation), hash pinning, byte caps, allowlist; cache-hit re-verified by re-hash |
| code.run | Bounded + isolated | namespace/rlimits/timeout/caps; approval-gated when destructive |
| approval | Exactly-once NOW | conditional-UPDATE consumption (CV-2 fix `74e1561`); expired decisions rejected; TTL config-driven |
| signals | Runtime-owned namespaces | `interface.*`/`work.*` unforgeable by the AI (enforced in `signals.py`, `allow_reserved=False`); watermark prevents retroactive wakes; waiter-safe pruning |
| deliveries | Durable + honest | pending/retrying/delivered/failed; exponential backoff; terminal, loud, metric-ed exhaustion; interface-agnostic horizon |
| heartbeats/leases | Real | lease 120s, reclaim-counts-as-attempt; `FOR UPDATE SKIP LOCKED` claim query; stale-lease recovery honest |
| fencing | Real | `expected_owner` conditional updates on mark_running/succeeded/failed — a stale worker cannot write over a reclaimed item |
| recovery | Honest | crash → lease expiry → reclaim → dead + dead-letter + `work.dead` signal; `recover_orphans` fails stale executions loudly (resume-from-checkpoint remains unwired — honest degradation, documented) |

## Coherence gaps (all classified, none silent)

1. Work-path approvals announce only on the ledger — the human may never learn (CV-12/14, shared-gate extraction designed).
2. `ResourceAccountant.release()` never called — allocations accumulate process-lifetime (DEAD, wire-at-finalize).
3. Cross-principal invocation: none found — ownership checked in work.cancel/requeue, memory.*, objective.*, message.send (credential-bound), approvals (credential-authenticated human).
4. `work.cancel` of mid-flight work: fencing discards results, but the external effect may have landed — inherent at-least-once, documented in `repository.py:14-17`. Acceptable.

## Verdict

COHERENT. One effect point, one gate chain (duplicated in form, identical in logic), untrusted paths resource-limited, every external effect audited, leases/fencing/watermarks/exhaustion honest. The five seams are local defects with designed fixes — none is an architecture fracture.
