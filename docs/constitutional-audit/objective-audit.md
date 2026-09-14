# Objective Audit — Constitutional Audit

Scope: `src/wax/objective/` (contracts, evidence, repository), `state/objective_models.py`, bridge + work-handler coupling, ADR-0020.

## State machine (verified)

`pending → in_progress → waiting / awaiting_human → succeeded / failed(→in_progress) / cancelled / abandoned`
- Transition map: `objective/repository.py:23-60`, enforced in `transition()` with DB-fresh reads (`refresh` before every decision).
- Terminal states immutable (except `failed → in_progress` retry, by design).
- Append-only `objective_executions` history (objective ≠ execution; one objective, many executions).

## Lifecycle answers

- **Creation**: bridge creates one objective per inbound interaction (kind=SINGLE_TURN), free-text description, interface metadata in context — domain-free shape.
- **Completion**: `objective.update_status` lets the model close `succeeded` with self-authored narrative evidence — runtime guards are transition-legality, terminal immutability, ownership, non-empty. This is an explicit ADR-0020 decision (§106-110), not a drift: the AI may *determine* completion when no explicit success criteria exist, and the runtime retains the evidence. ⚠ Awaiting_human→succeeded is legal, so the AI could declare success while an approval is pending — the approval survives independently and can still authorize; flagged as design tension for the founder, not silently accepted.
- **Evidence syncs** (runtime-owned, tolerant): work scheduled → waiting; woken/consumed → active; approval pending → awaiting_human; last-work death → failed. Cross-session staleness bug (identity-map copy) was found and fixed in the ADR-0020 cycle via DB-fresh reads.
- **The constitutional fix (CV-7)**: the bridge's auto-succeed used to transition unconditionally after `record_execution_end` — a swallowed sync could yield a PERMANENT `succeeded` with durable work still pending. Now `objective_has_outstanding_work` (DB truth) gates the terminal transition; outstanding work ⇒ waiting. **The runtime requires evidence, not model confidence — and not silence either.**
- **Waiting/awaiting_human/failure/cancellation drivers**: runtime evidence drives waiting/awaiting/failure; humans drive approvals; the requester drives cancellation.
- **Resumption**: `objective.resume` redirects a new execution into the objective and appends history. Reconstruction is metadata-level: pending work, artifacts, memories, objective text (now actually delivered to context — CV-6). ⚠ Checkpoint restore is stored-but-unread (`IMPLEMENTED-BUT-UNWIRED` — the designed next wiring target).
- **Stranded-state risk**: approval expiry via the maintenance sweep does NOT sync its objective → `awaiting_human` can strand forever if no other event arrives. No objective-reconciliation pass exists. Classified LIVE gap; designed fix: maintenance gains an objective-reconciliation sweep (expiry → failed/active per evidence).

## Hidden "task completed" assumptions searched for

None found: no code path transitions to `succeeded` without (a) the bridge's evidence-guarded auto-close or (b) `objective.update_status` under the principal's authority with recorded narrative evidence. The docstring contradiction (evidence.py claims "not model claims" while update_status allows narrative) is resolved as documented design tension — ADR-0020 explicitly decided it; this audit records it rather than relitigating it.

## Verdict

The objective subsystem is the strongest evidence-driven machine in WAX: real state map, immutable terminals, append-only history, DB-fresh transitions, runtime-owned syncs. After CV-6/CV-7 the two structural lies are gone. Remaining: stranded-`awaiting_human` sweep and checkpoint-resume wiring.
