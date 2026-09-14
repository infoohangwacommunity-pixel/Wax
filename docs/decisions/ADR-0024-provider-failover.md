# ADR-0024: Provider Failover — Ordered Candidates from Configuration

Date: 2026-09-14
Status: Accepted
Extends ADR-0009 (provider resilience).

## Context

Mission §33/§34: model selection must be generic infrastructure ("do
not hardcode a fixed routing policy"), and the runtime must survive
provider failure where possible ("Model A fails. Model B continues").
The audit at `3310ebb` found a single configured provider wrapped in
classified retry + breaker: a provider outage exhausted retries and
left the objective waiting for an operator to edit settings and restart.

## Decision

- **Candidates, not a policy engine.** `WAX_LLM_PROVIDER_FALLBACKS`
  (comma-separated provider kinds, default empty) defines an ordered
  candidate list after the primary. Each candidate is built at BOOT via
  the same settings-driven construction as the primary and wrapped in
  its own ResilientProvider (classified retry + per-provider breaker) —
  the same resilience contract for every vendor.
- **Failover is the composition of per-candidate resilience.** A
  candidate is abandoned only after its own retry budget exhausts (and
  its breaker opens on repeated failure). `complete()` walks the list;
  the FIRST success returns, and the response records which provider
  actually served (existing provenance). When every candidate fails,
  the LAST error is raised honestly — no fabricated success.
- **Boot-time loudness.** Unknown fallback kinds, or known kinds
  without their API keys, raise `WaxConfigurationError` at startup — a
  misconfigured fallback must be a boot error, not a 3am surprise.
  Duplicates of the primary (or of an earlier fallback) are skipped:
  failing over to the same broken provider helps nobody.
- **Streaming stays primary-only.** Streaming failover interleaves
  provider semantics mid-stream; the honest scope is documented rather
  than a fake universal claim.
- `close()` closes every candidate; `candidates` exposes the ordered
  list read-only for observability.

## Non-goals

- Cost/latency/modality-aware routing (mission §33 lists them as
  eventual inputs): no evidence yet for the weights; the candidate list
  is the infrastructure that a future selector would drive.
- Health-based reordering at runtime: breaker state already demotes a
  failing candidate implicitly (its retry budget is spent); explicit
  reordering adds a policy surface before there is evidence for one.

## Proof

- `tests/unit/test_provider_failover.py` — 5 tests: primary failure
  fails over to the fallback (and a healthy primary never invokes
  fallbacks), all-fail raises the last error honestly, parsing skips
  duplicates and rejects unknown kinds at boot, empty setting yields
  no candidates.
- Suite: 640 passing (625 before this batch). Live probe: PASS.
