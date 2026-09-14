# Reconciliation #4 — verified gap list (fourth principal-engineer pass)

Basis: repository at `848da4d` (518 tests verified passing locally; live
probe verified). Prior report treated as claims: 440 tests ✓ (verified),
9/9 probe ✓ (verified), 6 commits ahead ✓ (verified), worktree clean ✓.

The five documented boundaries were re-evaluated against the Universal
Primitive Test (would the mechanism still make sense if education,
WhatsApp, the model, the provider, the app, the interface, or the
capability disappeared?). ALL FIVE are infrastructure. All five are now
built; the pass also found and fixed three additional defects.

| # | Gap found | Category | Verdict | Fix + proof |
|---|---|---|---|---|
| 1 | Package acquisition recorded as design-only | dependency acquisition | BUILD | `workspace.acquire` capability: mandatory sha256 integrity, host allowlist, network-boundary egress, byte cap, content-addressed cache with hash-verified hits, atomic writes, owned-workspace isolation, provenance audit. 12 tests + ADR-0015 |
| 2 | Character-based context budget (no provider awareness) | context | BUILD | `wax.intelligence.context_limits` negotiation: provider-advertised limit − output reserve → char budget; configured fallback; floor for tiny windows; adapters advertise inside their own files (INV-03). 12 tests + ADR-0015 |
| 3 | Single-instance worker correctness gaps | durable execution | BUILD | Atomic claims (FOR UPDATE SKIP LOCKED), lease fencing on terminal writes, heartbeats (lease/3), reclaim metrics, bounded intra-process concurrency, graceful draining stop. 14 tests + ADR-0013 |
| 4 | No human-approval workflow | authority | BUILD | `pending_approvals` + ApprovalService + bridge approval gate + human decision path (`/approve <id>` grammar; credential-authenticated; AI can never decide) + one-time consumption + expiry sweep + `approval.*` runtime-owned signals. 16 tests + live probe + ADR-0014 |
| 5 | Signal ledger grows forever | lifecycle | BUILD | Deterministic retention + bounded storage with the waiter-safety predicate (a signal a pending event-wake can still fire is never deleted); runtime.maintenance loop. 10 tests + live probe + ADR-0015 |

## Additional defects found and fixed during the pass

| Defect | Fix |
|---|---|
| Reclaim-exhausted items died SILENTLY (no dead-letter row, no `work.dead` signal) | ClaimBatch carries the pass's deaths; runner announces dead-letter + signal in the same transaction |
| Expired waits produced no announcement — dependents could not react | `work.expired:<id>` signal emitted with the sweep |
| `stop()` cancelled the runner mid-flight, orphaning running items | Graceful draining stop with bounded grace period |
| Model↔migration drift (Python-side defaults vs server defaults) — `alembic check` failed | `server_default` declarations aligned; drift is now zero and tested |
| `work_schedule_impl` refused to schedule destructive work outright (pre-approval era guard) | Replaced: scheduling allowed; the wake-time approval gate enforces "no run without a human YES" |
| Approval flow in the durable-work handler lacked the consume path (a second pending was created instead of consuming the human's YES) | Handler now checks approved-unconsumed first — work runs exactly once per approval |

## Test counts

- Session start (verified): 440
- After this pass: **518** (+78: concurrency 14, approvals 16, ledger 10,
  context budget 12, acquisition 12, open-world wave 2: 9, migrations 5)
- Live probe: 12 PASS checks (was 9), exit 0

## Deliberate non-goals (re-evaluated, still legitimate)

- **Container/microVM isolation for code.run** — the subprocess boundary
  is honestly documented as accident-isolation, not a sandbox; stronger
  isolation is a deployment concern (Docker/gVisor) and the isolation
  CONTRACT is in place. Revisit if multi-tenant adversarial code becomes
  a workload.
- **Real tokenizer in the runtime core** — the negotiation surface is
  live; tokenizer-exact accounting lives inside adapters when a
  deployment needs it (INV-03 keeps it there).
- **Multi-node leader election for the maintenance loop** — sweeps are
  idempotent and safe to run concurrently; election adds a coordination
  dependency without a correctness need at current scale.
- **tsvector/embedding memory retrieval** — ADR-0010 portable scorer
  remains correct at current scale.
