# ADR-0020: Objective Lifecycle Completion — Evidence States, Execution History, and Intelligence-Facing Objective Capabilities

Date: 2026-09-14
Status: Accepted
Replaces the objective sections of ADR-0003's lifecycle wiring notes.

## Context

The master mission (§15–17, §52, §99, §101) requires objectives to be the
runtime's durable representation of *what the human wants to accomplish*,
with a lifecycle that reflects reality. The repository audit at
`4f182d7` found the objective record was a shell in three ways:

1. **States did not cover real conditions.** `pending → in_progress →
   succeeded | failed | abandoned` had no representation for "durable
   work exists and is waiting", "a human approval is pending", or "the
   principal cancelled this". A scheduled reminder and a forgotten
   intention looked identical in the data model.
2. **One objective = one execution.** A single mutable `execution_id`
   column with no history erased the execution trail (retries,
   resumptions, multiple interactions) that mission §16 explicitly
   requires the runtime to represent.
3. **The intelligence could not see or drive objectives at all.** The
   bridge created a NEW objective per inbound message, and there was no
   capability surface for objectives. Every long-running human goal was
   therefore a chain of sibling single-message objectives — continuity
   by accident (memory retrieval), not by representation (mission §99:
   a resumed objective reconstructs objective + checkpoint + pending
   actions, then asks "what is the correct next action?").

## Decision

### 1. Lifecycle states are extended to the conditions the runtime can actually evidence

```
pending ──→ in_progress ──→ succeeded
   │             │  ^            (terminal)
   │             │  └── waiting ──┘ (evidence: durable work pending)
   │             │  └── awaiting_human (evidence: approval pending)
   ├──→ waiting ─┤
   ├──→ awaiting_human
   ├──→ cancelled  (terminal — active decision)
   ├──→ abandoned  (terminal — drift)
failed ──→ in_progress (retry)
```

- `waiting`: linked durable work exists and is not yet resolved. The
  bridge's interaction-level `succeeded` transition is REFUSED while an
  objective is waiting — an interaction that scheduled work is not a
  completed goal (mission §60: "Never: 'I'll remind you tomorrow'
  unless durable work actually exists"; conversely, durable work
  outstanding means the objective is not done).
- `awaiting_human`: a pending approval exists whose provenance traces
  to this objective's execution.
- `cancelled`: an active decision under the principal's authority,
  distinct from `abandoned` (drift) and `failed` (honest error).

Terminal states accept no further transitions; `succeeded` remains
closed exactly as before.

### 2. Objective ≠ execution: append-only participation history

New table `objective_executions` (migration `b9c1d3e5f7a2`): one row per
(objective, execution) participation — `kind` (`bridge` | `work`),
`started_at`, `ended_at` (NULL = open), `outcome` (`succeeded | failed |
cancelled | superseded`). The `objectives.execution_id` column remains
the *current*-execution pointer. A resumed objective's history spans
every interaction and work run that served it (§99 resumption evidence).

### 3. The runtime syncs objective state from evidence, without intelligence in the loop

`wax.objective.evidence` is the single synchronization point:

| Runtime fact | Objective sync |
|---|---|
| `work.schedule` under the objective's execution | → `waiting` |
| Woken work claimed / approval consumed | → `in_progress` |
| Pending approval created (bridge or work path) | → `awaiting_human` |
| Last outstanding work died or its wait expired | → `failed` (only if nothing else of that execution is still outstanding — one dead reminder must not kill an objective with other live work) |

All syncs are **tolerant by contract**: they resolve the objective
through the execution history (falling back to the current pointer),
they never raise into the primary flow, and they only move through
transitions the map already allows. **Cross-session freshness is part of
the contract**: capability invocations run in their own session, so
every transition/sync re-reads the current status from the database
(`session.refresh`) — deciding on a stale identity-map copy let an
interaction close an objective whose work was still outstanding (found
by the live-path test `test_scheduled_work_holds_the_objective_open`).

### 4. The intelligence gets a bounded objective surface

Through the standard gate chain (agency → budget → authority → invoker
→ audit), under the principal's authority:

- `objective.list` (`objective.read`): the principal's objectives with
  status and execution counts. The model can discover ongoing work
  before assuming a request is new.
- `objective.resume` (`objective.write`): attach the CURRENT
  interaction's execution to an existing non-terminal objective of the
  same principal. The prior per-message objective is closed as
  `cancelled` with outcome `superseded` in its history; the resumed
  objective records a timestamped resume note. This is the mechanism
  that makes "continue that" continue the *same* objective.
- `objective.update_status` (`objective.write`): close with
  `succeeded | failed | cancelled | abandoned` — evidence REQUIRED. The
  runtime records the claim with its evidence in the objective context
  (mission §24: the runtime retains the evidence supporting the
  completed state; it does not verify intent, it preserves the claim +
  evidence for audit). Terminal objectives are immutable.

New permissions `objective.read` / `objective.write` join the manifest
(source-of-truth derivation and drift guard already in place); the
`member` and `admin` roles grant them; the `ai` role remains zero.

## Consequences

- Objective state is now truthful under all verified flows: scheduling
  holds it open, approvals surface as awaiting-human, consumed
  approvals reactivate, last-work death fails it.
- The per-message objective remains the default; `objective.resume`
  makes continuation an explicit, auditable act by the intelligence.
- The bridge's interaction-level close may now be refused (waiting →
  succeeded is illegal); the refusal is a loud log line, the response
  itself is unaffected.
- No domain concepts were introduced: states and capabilities are
  generic; the description remains free text the intelligence
  interprets (INV-01).

## Non-goals

- Auto-completing objectives when their work succeeds: completion is a
  judgment with evidence (the intelligence's or the human's), not a
  side effect. A fired reminder is evidence; the goal's completion
  belongs to `objective.update_status`.
- Objective merging/splitting: no evidence of need yet; resume already
  covers the continuity case.

## Proof

- `tests/integration/test_objective_lifecycle.py` — 16 tests: transition
  map edges (including illegal waiting→succeeded and terminal closure),
  multi-execution history, open-row counting, live-path evidence walks
  (scheduled work holds the objective open; destructive scheduled work
  walks waiting → awaiting_human → active with exactly-once execution),
  capability surface (list with history counts, cross-principal
  non-leakage, resume spanning both interactions with live redirect,
  foreign/terminal rejection, evidence-mandated closure, terminal
  immutability).
- Suite: 601 passing (585 at cycle start). Live probe: PASS.
