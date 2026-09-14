# Constitutional Audit Report — WAX Runtime

**Date**: 2026-09-14 · **Auditor basis**: independent re-verification (no prior report trusted)
**Repository state audited**: `d639f6e` (640 tests, live probe PASS) → remediated through `bcec2a5` (644 tests, probe 13/13 PASS)

---

## 1. The constitution applied

Single governing sentence: **"The runtime provides mechanisms. The intelligence decides what to do with them."**

Every source file (121 Python modules, ~19,600 LOC), every database table (19), every capability (19 registered), every worker (5), every prompt, every state machine (10), every migration (11), and every runtime boundary was interrogated with two questions:

1. *Why does this exist?*
2. *Would it still deserve to exist if education, WhatsApp, or the current LLM disappeared?*

Passing tests were treated as evidence of behavior, **never** as proof of philosophical correctness. Five parallel audit tracks (memory; context+objective; capabilities+execution; provider+interface+prompt; database+config+state machines) traced reality, not documentation.

## 2. Overall verdict

**The spine is constitutional.** WAX's capability layer is genuinely what the constitution demands: 19 mechanisms, zero features. Reminders, waits, dependencies, and approvals are emergent compositions — `tests/integration/test_work_runtime.py::test_reminder_without_a_reminder_service` proves the negative (a reminder with no reminder service anywhere). No education/tutoring ontology exists in runtime code (grep-verified; hits are comments asserting absence). Objectives, work, approvals, deliveries, and memory are evidence-driven, principal-scoped state machines. The system prompt is six lines of orientation. Security lives in runtime gates, not prompt text.

**The drift was real but localized** — concentrated in *honesty-of-mechanism* failures: things that claimed more than they did. Ten constitutional violations were found and classified (see `constitutional-violations.md`); the six most severe were **permanently corrected** in five remediation batches (`47887cb`, `74e1561`, `f44a067`, `67357e0`, `bcec2a5`), each with tests, live probe, and push.

## 3. What was corrected (summary — details in the domain documents)

| # | Violation | Correction |
|---|---|---|
| CV-1 | Meta's 24-hour window + `"whatsapp"` default lived in the runtime capability layer, misapplied to every interface | `DeliveryPolicy`: interfaces **declare** their own vendor policy at the adapter wiring point; the runtime enforces whatever is declared, generically; `message.send` derives the interface from the principal's verified credentials; vendor rule invented by the runtime = zero |
| CV-2 | Approval "exactly-once" consumption was read-then-write (racy); expired approvals were decidable | Single conditional UPDATE decides consumption by rowcount; `decide()` rejects expired approvals; expiry reads the configured TTL |
| CV-3 | `memory.read`/`memory.write` permissions declared, granted, checked by **nothing** | Memory capabilities now require them (principal's authority; AI role stays empty) |
| CV-4 | INV-02/INV-03 claimed enforcement by architecture tests that **did not exist** | Both tests now exist as AST import scans and fail the build on coupling |
| CV-5 | Invoker docstring promised input-schema validation; none existed | Declared-contract validation is real code (required/types/lengths/bounds/enum) |
| CV-6 | The priority-0 OBJECTIVE context section could never fire — its DB link was written by nobody | Bridge writes `conversation.attach_objective` when both ends exist |
| CV-7 | Bridge could mark an objective `succeeded` while durable work was pending (permanent lie) | Evidence guard queries DB truth before any terminal transition; work outstanding ⇒ objective stays/becomes `waiting` |

Additionally fixed: `memory.forget` now honors its `idempotent=True` contract; supersede failures are loud in-transaction; the `sensitivity` column's false behavioral claim replaced with an honest RESERVED marker; dead-letter timestamps UTC; Anthropic provider no longer shares the OpenAI-compatible base URL; the triplicated interface↔credential mapping unified into `wax.identity.contracts` as the single source of truth.

## 4. The open-world test (mandatory)

Users: student, teacher, lawyer, doctor, engineer, researcher, business owner, parent, writer, unpredicted objective.

- **Memory, context, objectives, capabilities, delivery, approvals, waiting** are all domain-free (verified per-domain; see context-audit/capability-audit).
- A lawyer resuming a case objective receives: the objective itself (now actually — CV-6 fix), outstanding work metadata, artifact metadata, budgeted relevant memory, conversation recency. No education assumptions anywhere in that path.
- A doctor's "remind me to follow up" needs **no feature**: durable work + time wake + message.send + delivery queue compose it (proven by test).
- Remaining composition gaps are *mechanism-shaped*, not feature-shaped — none require a domain feature to close.

## 5. Universal Primitive Test — survivors

Every production file was required to justify its existence. Result: every runtime module passes (mechanism-shaped, domain-free). The flagged population is **unwired/dead code**, not domain code — full inventory with dispositions in `hardcoding-inventory.md` §3. None were deleted wholesale in this pass: honest-but-unwired mechanisms are classified `IMPLEMENTED-BUT-UNWIRED` or `DEAD` with a documented wiring-or-removal plan (several become load-bearing next cycle: checkpoint resume feeds objective resumption; media pipeline feeds interface multimodality), and none may be presented as working in user-facing documentation — the overclaiming docstrings/comments were the violation and are corrected.

## 6. Document map

| Document | Scope |
|---|---|
| `hardcoding-inventory.md` | every hardcoded decision, classified |
| `runtime-boundary-audit.md` | module boundaries + what enforces them |
| `memory-deep-audit.md` | the 20 memory questions, traced |
| `context-audit.md` | assembly, budget, degradation |
| `capability-audit.md` | all 19 capabilities interrogated |
| `objective-audit.md` | lifecycle + evidence discipline |
| `provider-audit.md` | model independence |
| `interface-audit.md` | WhatsApp leakage + canonical events |
| `database-audit.md` | all 19 tables, every column's purpose |
| `execution-audit.md` | one coherent execution model? |
| `prompt-audit.md` | every prompt classified |
| `constitutional-violations.md` | the formal violation register |

## 7. Remaining unresolved philosophical questions (founder-level, per mission §119)

1. **Sensitivity wiring** — memory `sensitivity` is honestly marked reserved. Deciding what sensitivity *does* (retention? evidence-assembly exclusion? approval for storage?) is a privacy-policy decision, not an engineering one.
2. **Retention regime** — 18 of 19 tables grow forever (only `runtime_signals` prunes). Whether verbatim conversation text may be pruned while preserving memory/audit integrity is a founder policy on data lifetime.
3. **Dynamic capability registration** — the registry is closed at boot by design (honest). Opening it to safe third-party acquisition is the capability-acquisition frontier; its security model deserves an explicit founder decision.
4. **Multi-instance security state** — rate/cost/abuse guards are process-local (self-admitted). Durable work is multi-instance-honest; hardening the security perimeter to match needs a deployment decision (Redis/Postgres backing).
