# WAX Evaluation Report (OMEGA)

Mission §54–§57: unit tests are insufficient — behavior must be
evaluated scenario-by-scenario. This report states exactly what is
covered, with the suites that prove it, and what is not. Counts are
from the OMEGA cycle run: **655 tests, all passing**.

## Memory + context evaluation (§54) — 10 scenario dimensions, LIVE

`tests/evaluation/test_memory_evaluation.py` runs the full decathlon
against the real retrieval + linking machinery:

| Dimension | Scenario |
|---|---|
| Recall | the important thing IS found among distractors |
| Irrelevance | an unrelated query retrieves nothing (no filler) |
| Update | new evidence supersedes; current truth wins; history survives |
| Temporal reasoning | observation time orders beliefs; "was true" ≠ "is true" |
| Contradiction | conflicting evidence coexists, linked, never silently destroyed |
| Abstention | no evidence means no fabrication |
| Privacy | one principal never retrieves another's memory |
| Forgetting | forgotten memory is gone from every retrieval path |
| Consolidation | derived knowledge traces to its evidence (provenance kept) |
| Retrieval poisoning | poisoned content stays labelled data |

Plus focused suites: memory mechanisms (TTL expiry), typed links
(creation, expansion, cross-principal refusal), context sections
(priority + budget behavior), retrieval upgrade (BM25 + scoring).

## Open-world evaluation (§55) — 13 scenarios, LIVE

`test_open_world.py` / `_2.py` / `_3.py` run founder-unhardcoded
objectives end-to-end over the real bridge + provider: research-style
comparison, document analysis, long-running tracking via
wait/signals/delivery, preference change (supersession), forgetting on
request, continuation across sessions, and unknown-domain objectives —
each asserting the runtime composed generic primitives with no
per-domain handler.

## Failure and recovery evaluation (§56) — 32 tests, LIVE

- `test_work_concurrency.py` (14): lease contention, fencing (stale
  owner cannot write), concurrent claim (SKIP LOCKED), exactly-once
  effects under races, orphan recovery.
- `test_reliability.py` (18): provider failure → classified retry →
  breaker → failover; partial execution; interrupted work; duplicate
  request (idempotency); stale state; delivery retry chain and
  horizon expiry.
- `test_maintenance_leadership.py` (+ this cycle's additions): leader
  election, follower skip, conversation lifecycle.

## Security evaluation (§57) — 18 tests + live probe, LIVE

`test_security_hardening.py`: cross-principal access (memory, work,
objectives, workspaces, messaging), replay (approval consumption),
forged approvals/decisions, workspace escape attempts, secret exposure
in logs, prompt injection marking, SSRF (scheme/IP/redirect), webhook
signature forgery. The live probe adds the real-path equivalents:
bad-signature webhook, destructive action requiring a human decision
delivered over the actual webhook grammar, exactly-once execution.

## Architecture tests — 7, LIVE

Composition-root and boundary tests: core has no provider/SDK imports;
interface bridge importable only from the boundary; provider SDKs only
inside adapters; adapter package reachable only via the factory
(CV-4's phantom enforcement made real in `bcec2a5`).

## Coverage gaps (honest)

1. No load/soak evaluation (N-replica) — the deployment envelope is
   single-instance for the enforcement perimeter (ADR-0027); a load
   evaluation would need that decided first.
2. No long-horizon (weeks) continuity evaluation — the durable state
   is tested at hour-scale; calendar-time behavior is config, not
   physics, so the honest gap is small but real.
3. Open-world scenarios are provider-mocked (deterministic); the live
   probe covers the real HTTP path but only for the flows the probe
   owns. A live-LLM evaluation harness is future work by design (cost,
   determinism).
4. Failure injection covers provider/worker/db-restart paths via
   integration tests; process-kill mid-commit is covered implicitly
   (durable acceptance checkpoint) but not as a dedicated fault-injection
   suite.
