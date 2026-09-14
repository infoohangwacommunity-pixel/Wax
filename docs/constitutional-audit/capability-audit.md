# Capability Audit — Constitutional Audit

Scope: all 19 registered capabilities, the registry model, scheduling philosophy, the agentic scan.

## Inventory (name | universal? | composable? | hides domain logic? | class)

| Capability | Universal | Composable | Domain logic | Class |
|---|---|---|---|---|
| `echo` | yes (fixture) | yes | no | LIVE (test scaffolding) |
| `http.get` | yes | yes | no | LIVE |
| `work.schedule` | yes — the reminder enabler (time+event wakes) | yes | no | LIVE |
| `work.cancel` | yes (ownership-checked) | yes | no | LIVE |
| `work.list` | yes | yes | no | LIVE |
| `work.requeue` | yes (dead-work recovery) | yes | no | LIVE |
| `message.send` | yes — after CV-1 fix (interface derived from identity; interface-declared policy) | yes | no (was: Meta policy) | LIVE (fixed) |
| `scratch.workspace` | yes | yes | no | LIVE |
| `signal.emit` | yes (reserved namespaces runtime-enforced) | yes | no | LIVE |
| `code.run` | yes | yes | no | LIVE |
| `workspace.acquire` | yes (integrity-pinned acquisition) | yes | no | LIVE |
| `approval.list` / `approval.cancel` | yes | yes | no | LIVE |
| `memory.store` / `memory.search` / `memory.forget` / `memory.consolidate` / `memory.link` | yes | yes | no | LIVE (permissions now enforced — CV-3) |
| `objective.list` / `objective.resume` / `objective.update_status` | yes (free-text, no kinds) | yes | no | LIVE |

## The reminder test (scheduling philosophy)

**Reminders are NOT a feature.** Grep for `reminder|tutor|lesson|study|exam` across `src/` returns only comments asserting absence and `core/invariants.py:45-48` ("Universal runtime mechanisms must not require education"). The composition is proven end-to-end: `test_work_runtime.py::test_reminder_without_a_reminder_service` — a user message → the AI schedules durable work → the runtime wakes it → message.send delivers. The runtime knows only: there is work, there is waiting, there is an event, there is delivery. **SCHEDULING: CLEAN** — the only timers are generic hygiene loops (poll 2s, maintenance 300s, reapers 60/300s).

## Registry model

Closed catalog at boot: `RuntimeServices.build()` registers everything; `register/unregister` APIs exist but nothing calls them post-boot. Dynamic extension is honestly deferred (documented in `registry.py`), and its opening is founder question #3. Discovery IS exposed to the model (capability tools surface); extension is not.

## Structural findings (non-violation, actioned)

1. Gate-chain duplication: bridge and work-handler each implement the approval/authorization chain; they already drift (bridge notifies humans of approvals, work path announces only on the ledger → a human may never learn of a work-created approval). → CV-12 in the register: extract one shared gate.
2. `idempotency_key` accepted on invocation requests, never honored → documented as false-contract; either honor or drop (next batch).
3. `objective_list_impl` N+1 (`list_executions(limit=200)` per row) — performance, bounded.
4. In-memory rate/cost/abuse state is process-local (self-admitted) — multi-instance security perimeter is a deployment decision (founder question #4).

## Verdict

Nineteen mechanisms, zero features. The capability layer is constitutionally the strongest subsystem in the codebase: ownership checks everywhere, one sole effect point (the invoker), honest unavailability, and emergent (not built) behaviors. After the CV-1/CV-3/CV-5 fixes, no capability hides application logic.
