# WAX Constitutional Audit — Current Reality (OMEGA Cycle)

Status: **no open constitutional violations**.
This document is the single-file summary the mission asks for (§7); the
full 13-document evidence set lives in `docs/constitutional-audit/`, and
the residual decision boundaries are ADR-0025/0026/0027.

Baseline this audit was performed at: branch `main`, clean tree,
HEAD == origin/main; **655 tests passed**; **13/13 live-probe checks
PASS over real HTTP**; migration head `f2b4d6a8c0e2`; 122 source files,
~20,300 LOC; 19 tables; 21 registered capabilities; 27 ADRs.

## Method

Every claim below was verified against code, tests, migrations, and the
live probe — not against previous reports. Three read-only audit passes
covered (1) every table's retention behavior and every sensitivity
site, (2) the capability registry, its contract coverage, environment
negotiation, isolation, and revocation, (3) every piece of process-local
state in the runtime. Findings were classified with the honest labels
(LIVE / IMPLEMENTED-BUT-UNWIRED / TEST-ONLY / DOCUMENTATION-ONLY /
PLACEHOLDER / DEAD / CONSTITUTIONAL VIOLATION) before any code changed.

## Result by mission area

| Area | Verdict | Evidence |
|---|---|---|
| Durable work (leases, fencing, recovery, retries) | LIVE | `work/repository.py`, `test_work_concurrency.py`; leader-guarded sweeps |
| Objective lifecycle (evidence-driven, ADR-0020) | LIVE | history table + DB guard against fabricated `succeeded` (`67357e0`) |
| Delivery integrity (ADR-0021) | LIVE | DeliveryQueue pending→retrying→delivered/failed; failures from ALL sources now become durable state (CV-13 fix) |
| Memory substrate (typed, links, importance, observed_at — ADR-0022) | LIVE | 18 memory tests + links suite; soft delete declared, not faked |
| Context as operational reality (ADR-0023) | LIVE | priority sections, budgeted, provenance-carrying; OBJECTIVE section reachable on the live path (CV-6 fix) |
| Provider independence (ADR-0009/0024) | LIVE | failover chain, per-candidate breakers, separate vendor base URLs (CV-10 fix) |
| Interface independence (ADR-0002/LAW 4) | LIVE | interface policy declared by the adapter (CV-1 fix); no WhatsApp types in the core (AST-tested) |
| Human approval primitive | LIVE, hardened | exactly-once conditional-UPDATE consumption; atomic creation via partial unique index (CV-11 fix); ONE shared gate for live + work paths (CV-12/14 fix) |
| Isolation honesty | LIVE | namespace sandbox with loud fallback; container/microVM/browser kinds honestly "not implemented" |
| Prompt architecture | LIVE | minimal runtime contract; evidence as separate labelled lines; untrusted content marked; enforcement never prompt-owned |
| Capability registry | CLOSED-BY-DESIGN | 21 capabilities, boot-time registration, honest descriptor gaps recorded in ADR-0026 |
| Retention | DECISION-BOUNDARY | 18/20 tables honestly unbounded; no policy invented — ADR-0025 |
| Multi-server state | CLASSIFIED | durable core correct; enforcement perimeter single-instance — ADR-0027 |
| Observability | LIVE | execution steps, audit events, signals, metrics, logs — redaction enforced |

## Violations corrected across both audit cycles (14 total)

- CV-1 vendor policy in runtime → adapter-declared `DeliveryPolicy` (`47887cb`)
- CV-2 approval read-then-write consumption → conditional UPDATE (`74e1561`)
- CV-3 memory permission theater → descriptors enforced (`f44a067`)
- CV-4 phantom-enforced invariants → real AST architecture tests (`bcec2a5`)
- CV-5 invoker input validation promised, absent → enforced (`bcec2a5`)
- CV-6 priority-0 OBJECTIVE section unwritable on live path → bridge writes the link (`67357e0`)
- CV-7 tolerant evidence syncs could fabricate terminal `succeeded` → DB guard (`67357e0`)
- CV-8 sensitivity false claim → honest RESERVED (`f44a067`)
- CV-9 forget contract false → honest no-op for already-forgotten (`f44a067`)
- CV-10 shared vendor base URL → separate provider endpoints (`bcec2a5`)
- CV-11 approval creation race → database-owned idempotency (`b439aae`)
- CV-12 duplicated approval gate → one shared gate (`b439aae`)
- CV-13 delivery failure = dead letter → recoverable delivery state (`b439aae`)
- CV-14 background approvals silent → notified or durably retried (`b439aae`)

False mechanisms corrected beyond the register (same cycle `b439aae`):
conversation lifecycle claimed-but-unwired (now wired), dead
`MemoryStatus.ARCHIVED` state (removed), "append-only" ledger claim
(corrected), artifact `expires_at` mirror claim (corrected),
observability privacy claim (corrected), dead-letter `reprocessed`
claim (corrected), `MemoryRepository.unlink` ownership hole (closed).

## Open items (honest, designed, founder-visible)

1. Retention policy for the 18 unbounded tables — founder decision
   sheet in ADR-0025. No pruner written; none should be before policy.
2. Sensitivity semantics — RESERVED until a privacy policy exists.
3. Open capability registry — designed lifecycle + build order in
   ADR-0026; implementation deferred until evidence justifies.
4. Multi-instance enforcement perimeter + execution heartbeat —
   designed, deferred in ADR-0027; single-instance deployment is the
   current honest envelope.
5. `output_schema` validation, `idempotency_key` honoring, descriptor
   provenance — recorded in ADR-0026's ranked primitive list.
