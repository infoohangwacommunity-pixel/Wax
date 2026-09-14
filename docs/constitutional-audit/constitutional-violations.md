# Constitutional Violations — Register

Every finding classified `CONSTITUTIONAL VIOLATION`. Format: location / why it violates philosophy / evidence / fix / reversibility / confidence.

| ID | Location | Why it violates | Fix | Reversibility | Confidence |
|---|---|---|---|---|---|
| CV-1 | `capabilities/runtime_capabilities.py:44,585-613,248,559` (pre-fix) | Meta's 24-hour customer-service window and a hardcoded `"whatsapp"` interface default lived in the RUNTIME capability layer and were applied to every interface — vendor policy is not a runtime mechanism; the runtime must survive WhatsApp disappearing | **FIXED `47887cb`**: `DeliveryPolicy` declared by the interface adapter at the wiring point (`app.py`); runtime enforces declared policy generically; interface derived from principal credentials; mapping unified in `wax.identity.contracts` | High (pure relocation + derivation) | High (3 audit tracks converged) |
| CV-2 | `authority/approvals.py:245-269` (pre-fix) | Approval consumption was read-then-write: two concurrent gate passes could both consume one human decision — breaking "replay is impossible" in the authority primitive | **FIXED `74e1561`**: single conditional UPDATE (`WHERE status='approved' AND consumed_at IS NULL`); rowcount decides; `decide()` rejects expired approvals; TTL read from settings | High | High |
| CV-3 | `capabilities/runtime_capabilities.py` memory descriptors (pre-fix) | `memory.read`/`memory.write` existed in the permission manifest, were granted to roles, and were checked by NOTHING — authorization theater on the memory surface | **FIXED `f44a067`**: descriptors require `memory.write` (store/forget/consolidate/link) and `memory.read` (search); principal's authority enforced; AI role remains empty | High | High |
| CV-4 | `core/invariants.py:62,72` | INV-02/INV-03 declared enforcement by `tests/architecture/test_no_interface_coupling.py` and `test_no_provider_coupling.py` — files that did not exist. The constitution's own boundary claims were phantom-enforced | **FIXED `bcec2a5`**: both tests created (AST import scans: interface bridge importable only from the boundary + composition root; provider SDKs only inside adapters; adapter package reachable only via the factory) | High | High |
| CV-5 | `capabilities/invoker.py:11` (pre-fix) | Docstring step 5 promised input validation against `input_schema`; no validation existed — the declared contract was decoration (false mechanism at the SOLE effect point) | **FIXED `bcec2a5`**: `_validate_inputs` enforces required/types/lengths/bounds/enum before execution; implementations keep semantic validation | High | High |
| CV-6 | `continuity/service.py:151` + `contracts.py:51` (pre-fix) | The priority-0 OBJECTIVE evidence section was sourced from `ConversationRecord.objective_id`, which NO production code ever wrote — an advertised mechanism that could never fire, masked by hand-built test contexts (the exact "passing tests ≠ correctness" trap) | **FIXED `67357e0`**: bridge writes `conversations.attach_objective` when both ends of the link exist | High (additive wiring) | High |
| CV-7 | `bridge/service.py:396-399` + `objective/evidence.py:85-87` (pre-fix) | Tolerant (swallowing) evidence syncs + unchecked auto-succeed could fabricate a terminal `succeeded` while durable work was pending — a PERMANENT lie (terminal states immutable), violating "status must reflect RUNTIME EVIDENCE" | **FIXED `67357e0`**: `objective_has_outstanding_work` DB guard before the terminal transition; outstanding work ⇒ objective becomes/stays `waiting` | High | Medium-high (low probability, high damage, structurally closed) |
| CV-8 | `state/memory_models.py:103-104` (pre-fix) | `sensitivity` column claimed "Affects what gets logged, what gets surfaced, retention" — nothing reads it. A schema comment asserting a privacy control is a false mechanism in the safety-critical lane | **FIXED `f44a067`**: honest RESERVED marker + remediation documented in `memory-deep-audit.md`; actual wiring is a founder privacy-policy decision | High | High |
| CV-9 | `memory.forget` descriptor vs impl (pre-fix) | Descriptor declared `idempotent=True`; implementation raised on any non-active memory — the contract was false | **FIXED `f44a067`**: already-forgotten ⇒ honest no-op (`already_forgotten: true`); superseded memories still refuse loudly (real state conflict) | High | High |
| CV-10 | `intelligence/service.py:103,122-123,205,219` (pre-fix) | One shared `llm_base_url` fed BOTH vendor adapters: a mixed-vendor fallback chain silently pointed the Anthropic candidate at an OpenAI-compatible proxy — a hidden provider assumption | **FIXED `bcec2a5`**: separate `anthropic_base_url` setting; adapters no longer share a base URL | High | High |

## Documented-but-not-yet-remediated → **FIXED in the OMEGA cycle (`b439aae`)**

These four were tracked across cycles (the founder's §58 "items that
must not disappear") and are now fixed in code, each with tests:

| ID | Location | Class | Fix shipped |
|---|---|---|---|
| CV-11 | `authority/approvals.py create_or_get_pending` | Violation (medium) | **FIXED `b439aae`**: atomic INSERT..ON CONFLICT DO NOTHING against partial unique index `uq_pending_approvals_principal_fp_pending` (migration `f2b4d6a8c0e2`, downgrade drops exactly the index); race loser returns the winner's row; tested |
| CV-12 | `runtime/work/handlers.py` vs `bridge/service.py` gate chains | Violation (drift risk) | **FIXED `b439aae`**: ONE shared component (`wax/authority/gate.py` ApprovalGate) serves both paths; behavioral parity tested |
| CV-13 | Delivery sources: only `bridge_reply` enqueued durable delivery records | Violation (honesty gap, ADR-0021 partial) | **FIXED `b439aae`**: `message.send` transport failures enqueue a DeliveryRecord (source=`capability:message.send`, failed attempt counted) and return `sent=false, queued_for_retry=true` |
| CV-14 | `authority/approvals.py` notification asymmetry | Violation (human-authority visibility) | **FIXED `b439aae`**: the work path notifies through the shared gate — live channel first, durable retry state (source=`approval_notification`) when no adapter is up; live probe shows the notification attempt firing on the real path |

No constitutional violations remain open. All findings are classified
in the domain documents as LIVE / IMPLEMENTED-BUT-UNWIRED / TEST-ONLY /
DOCUMENTATION-ONLY / PLACEHOLDER / DEAD. Residual designed-but-deferred
boundaries are recorded in ADR-0025 (retention policy), ADR-0026 (open
registry), and ADR-0027 (multi-server enforcement perimeter).
