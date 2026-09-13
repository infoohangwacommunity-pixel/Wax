# ADR-0015: Artifact Acquisition, Ledger Retention, Context-Budget Negotiation

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX principal-engineer continuation session (fourth pass)

This ADR closes the three recorded non-goals that failed re-evaluation:
package acquisition, signal-ledger pruning, and the character-based
context budget. Each was re-tested against the Universal Primitive Test
and each is infrastructure, not application.

## 1. workspace.acquire — dependency acquisition as an environment mechanism

**Context.** ADR-0011 §Future recorded package acquisition as speculative.
Re-evaluation: "I need a dependency/resource for this work" is a basic
environment capability — a runtime that cannot bring resources into an
isolated workspace forces intelligence to fake it. The acquisition loop
bottoms out at (source, content-hash) for EVERY ecosystem; that is the
generic primitive.

**Research:** pip/npm/Cargo pin+verify via lockfiles (integrity is not
optional); TUF/in-toto (supply-chain security = verify before use);
content-addressed stores (Nix, Git) — the hash IS the identity; SSRF
lessons already encoded in WAX's network boundary (ADR-0008).

**Decision:**
- `workspace.acquire(workspace_resource_id, url, sha256, filename?)` —
  the artifact lands ONLY inside an active scratch_dir owned by the
  requesting principal (containment verified before write).
- Integrity is MANDATORY: the exact SHA-256 must be declared up front;
  mismatch refuses and leaves NOTHING (no temp residue).
- Source policy is deployment config (`WAX_ACQUISITION_ALLOWED_HOSTS`,
  default PyPI/npm/GitHub file hosts; empty = disabled, honestly
  reported). Non-allowlisted hosts refuse before any bytes move.
- Egress crosses the network boundary (ADR-0008): SSRF-guarded,
  per-hop redirect re-validation, hard byte cap, hard timeout.
- Content-addressed cache under the provisioning root, keyed by sha256;
  a cache hit is re-verified by hashing; a corrupted entry is deleted
  and re-fetched (no silent supply-chain drift). Writes are atomic
  (temp + fsync + replace).
- Provenance: `artifact.acquired` audit event (host, hash, bytes,
  cache_hit, workspace) per acquisition.
- The artifact is DATA: acquisition never executes it (no install
  hooks). Import/execution belongs to `code.run` under its own
  authority and isolation — pure-python wheels import via `sys.path`
  without running any setup code.

**Non-goal preserved:** WAX does not become a package MANAGER. No
dependency graphs, no transitive resolution, no virtualenvs — the
intelligence resolves; the runtime verifies and isolates.

## 2. Signal-ledger retention — deterministic, waiter-safe pruning

**Context.** The ledger was append-only forever. Unbounded growth is a
resource leak; careless pruning silently changes wait semantics.

**Research:** Kafka log compaction/retention (bounded, deterministic
cleanup); Postgres queue-practice pruning with safety predicates; the
watermark design of ADR-0011 — the key insight is that wait state lives
on the WORK ITEM, not the ledger.

**Decision:**
- Retention pass: signals older than `WAX_SIGNAL_RETENTION_SECONDS`
  (default 30 days) are pruned — UNLESS a pending event-wake item's
  watermark predates the signal (that signal can still fire the wait;
  it is NEVER deleted).
- Bounded storage: beyond `WAX_SIGNAL_MAX_LEDGER_ROWS` (default 100k),
  the oldest prune-safe rows go, capped per pass.
- Determinism: the pass deletes by explicit id list ordered by age,
  is idempotent, logs counts + age ranges, and meters
  `runtime_signals_pruned_total`.
- Replay semantics are unchanged by construction: pruning only removes
  rows no pending waiter can be woken by; a waiter created later has
  watermark=now and could never have been woken retroactively.

## 3. Context-budget negotiation — provider limits without provider coupling

**Context.** ADR-0012 used a fixed character budget (24k chars ≈ 6k
tokens) — honest but blind: the same budget serves an 8k-token model and
a 200k-token one. ADR-0012 recorded the negotiation as future work.

**Research:** capability negotiation (the runtime asks what the
component can do); tokenizer-free estimation practice (chars/token ≈ 4
is the standard conservative fallback); the INV-03 boundary — tokenizer
knowledge may live inside an adapter file, never in the core.

**Decision:**
- `wax.intelligence.context_limits`: duck-typed negotiation. If the
  provider advertises `context_limit_tokens`, the evidence budget is
  derived: (limit − `WAX_LLM_OUTPUT_RESERVE_TOKENS`) × 4 chars/token.
  If not, the configured fallback applies. A tiny advertised limit hits
  an honest floor (never an empty context).
- Adapters advertise inside their own files: OpenAI per-model table,
  Anthropic 200k, mock configurable for tests. A real tokenizer
  (tiktoken) may replace an adapter's estimator without contract change.
- The resilience wrapper is transparent to negotiation (inner-provider
  unwrapping); a broken or absent advertisement degrades gracefully —
  budgeting errors always fall on the safe side.
- The assembler contract (ADR-0012) is unchanged: it fills whatever
  budget it is given, prioritized, with announced truncation. Malformed
  evidence entries are coerced or dropped honestly, never fatal.

## Evidence

- `tests/integration/test_workspace_acquire.py` (12 tests)
- `tests/integration/test_ledger_lifecycle.py` (10 tests)
- `tests/unit/test_context_budget.py` (12 tests)
- Live probe: retention pass prunes the live ledger (12 PASS checks total)
