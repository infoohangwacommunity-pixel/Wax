# ADR-0010: Memory as a Mechanism — Retrieval, Lifecycle, Capability Surface

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX principal-engineer continuation session

## Context

Memory existed as storage (Phase F) with three verified defects:

1. **Context assembly was a telephone book.** The bridge showed the AI the
   last five EPISODIC rows — recency without relevance. "What did I tell
   you about my exam?" surfaced whatever was newest, not the weeks-old
   exam fact. The Foundation PDF (§14) names retrieval as a runtime
   responsibility.
2. **Expiry never fired.** `expire_due()` existed; nothing called it.
   Memories with TTLs lived forever — a resource leak and a privacy
   failure (retention is a runtime concern; Directive privacy phases
   agree).
3. **The AI had no memory agency.** It could not deliberately persist a
   fact, search its own evidence, or honor "forget that" — only implicit
   episodic writes happened. The runtime owned storage but offered no
   mechanism for intentional memory work.

## Decision

- **Retrieval:** `MemoryRepository.search_relevant` scores a bounded
  candidate pool — term overlap weighted by recency decay and the
  record's confidence — across ALL kinds. `ContinuityService` composes a
  recency pool and a relevance pool (deduped, capped); each entry carries
  its reason (`recent` / `relevant` / `recent+relevant` + score) so the
  model can weigh evidence instead of trusting an opaque selection. The
  scoring is portable (no DB extensions); the documented upgrade path is
  a Postgres tsvector candidate fetch behind the same contract.
- **Lifecycle:** a lifespan worker (`memory/lifecycle.py`) forgets
  expired memories on a sweep — soft delete, audit event, metric.
  Idempotent; a restart loses nothing.
- **Capability surface:** `memory.store` / `memory.search` /
  `memory.forget` are ordinary capabilities crossing the same gate chain
  as every effect (agency → budget → authority → invoker → audit).
  `forget` is destructive-gated and ownership-checked (a principal
  cannot forget another's memory). Provenance on store is
  `model_observation` with `source_execution_id` — every stored fact is
  traceable to the execution that claimed it.

## What this deliberately is NOT

- No embedding/vector dependency, no frozen taxonomy of "profile fields".
  Content is structured evidence the AI interprets (Foundation PDF §14:
  "The AI interprets. The runtime stores."). If embeddings become
  necessary, `search_relevant`'s contract absorbs them.
- No automatic consolidation policy. Whether an episodic exchange yields
  a durable semantic fact is a decision the AI makes through
  `memory.store` — the runtime records it faithfully, with provenance.

## Consequences

- Context assembly quality is now an inspectable mechanism with tests,
  not a lucky byproduct of row order.
- Forgetting is guaranteed by the runtime, satisfying retention/privacy
  without trusting model behavior.
- Memory agency completes the AI's toolchain for open-world objectives:
  remember, recall, and withdraw are now runtime-gated verbs.
