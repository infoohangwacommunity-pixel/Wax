# Reconciliation #3 — verified gap list (third principal-engineer pass)

Basis: repository at `49c90e1` (408 tests verified passing locally; live probe
verified PROBE PASS exit 0). Every gap below names its source evidence. This
file is the working list; closed items move to the audit-response addendum.

**STATUS: all six gaps closed in this cycle** — commits `2ace7c4` (G1),
`9dd4a73` (G2), `430c334` (G3), `0ab9522` (G5), `648957e` (G4 fix within
open-world commit + probe), docs commit (G6). See audit-response Addendum 2
for the disposition table with proofs. ADR-0011 (durable waiting) and
ADR-0012 (memory lifecycle + context assembly) record the decisions.

## Verified gaps (to implement this cycle)

### G1 — Durable waiting is a timer, not a condition  (mission §14)
Evidence: `WorkItemRecord` has only `wake_at`/`available_at` timestamps;
`WorkRepository.claim_due()` selects `available_at <= now`. Work can wait for
a TIME and nothing else. The mission: waiting conditions include "another
event, a dependency becoming available, an external service response, a
resource becoming available, a human response, another execution completing".
Design: persisted signal ledger (`runtime_signals`), generic wake conditions
(`wake_kind` = time|event), per-item watermark, optional deadline, runtime-
owned signal namespaces, `signal.emit` capability, runner emits terminal-work
signals, bridge emits interface-message signals.

### G2 — Memory revision/consolidation exists at storage level only  (§8/§10)
Evidence: `MemoryRepository.supersede()` exists but no capability path calls
it; `memory.store` always creates fresh records, so contradictions between two
ACTIVE records are never resolved into supersession chains; there is no
consolidation mechanism (transient evidence → durable representation with
provenance). Design: `memory.store(supersedes=...)`, new `memory.consolidate`
capability (multi-source → one durable record, sources superseded, ownership
enforced, audit retained). The intelligence interprets; the runtime enforces
lifecycle.

### G3 — Context assembly discards the evidence it collected  (§12/§26)
Evidence: `ContinuityService.build_context()` fetches `conversation_summary`,
`active_objective_description`, `days_since_last_message`,
`last_execution_status` — but `_build_messages()`/`_build_system_prompt()`
deliver NONE of them to the model. On a resumed conversation the AI cannot
see the objective it is pursuing. Also no budget discipline (fixed 5/5/8
counts, no size accounting). Design: environment-mechanism context assembly —
evidence sections, per-section budget, ranked fill, honest truncation.

### G4 — Scheduled work loses its execution trace  (§26)
Evidence: `work_schedule_impl` passes `execution_id=None` although
`InvocationContext.execution_id` is available — work items cannot be traced
back to the execution/objective that scheduled them. Fix: pass the context
through (execution → objective linkage via the executions table).

### G5 — Dead work is a graveyard, not a recovery state  (§25)
Evidence: `DeadLetterRepository` has `record/list_recent/mark_reprocessed`;
`mark_reprocessed` has no caller; no mechanism re-drives dead work. A provider
outage that exhausts retries leaves objectives permanently dead with no
runtime path back. Design: `work.requeue` capability — owner requeues their
own dead item as a fresh work item (`requeued_from` provenance; the dead item
stays for audit).

### G6 — `docs/engineering/handoff.md` is stale (documentation)
Evidence: claims commit `da52c23`, 208 tests, "Phase R PROPOSED",
"WhatsApp placeholder runtime_callback" — all false at `49c90e1`. Rewrite to
current reality.

## Investigated, deliberately NOT built (evidence-based non-goals)
- **Dependency/package acquisition service (mission §18)**: the acquisition
  flow exists as `scratch.workspace` (provision) + `code.run` (execute, env
  scrubbed, network governed by the same egress rules as other capabilities).
  A package-acquisition mechanism (allowed sources, integrity, cache,
  reproducibility) is justified only when a real workload needs dependencies
  that neither the runtime image nor code.run can reach; building it now
  would be speculation. Design constraints are recorded in ADR-0011 §"Future".
- Multi-replica workers, tsvector/embedding retrieval, human-approval
  workflow — prior non-goals (audit-response addendum) stand.
