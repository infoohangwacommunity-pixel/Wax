# ADR-0009: Provider Resilience and the Proven Model Abstraction

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX principal-engineer continuation session

## Context

Two findings from the reconciliation pass:

1. **IMPLEMENTED-BUT-UNWIRED (the audit's core category again):**
   `CircuitBreaker` and `retry_with_backoff` existed with passing tests,
   but `IntelligenceService` called providers bare. One transient 429 or
   network blip failed the whole execution; a downed provider accumulated
   full timeouts across every execution — a retry storm waiting to happen.
2. **Model independence was asserted, not proven.** Only mock + OpenAI
   adapters existed; selecting `anthropic` raised "not implemented". A
   claim of provider-neutrality that has never survived a second provider
   with a genuinely different wire protocol is a hypothesis, not a
   property.

## Decision

- **`ResilientProvider`** wraps every provider selected by
  `from_settings`: classified retry INSIDE (transient: timeouts,
  transport errors, 408/409/429/5xx; permanent: other 4xx — retrying
  auth failures is harm), circuit breaker OUTSIDE (sustained logical-call
  failure opens the circuit and fails fast; HALF_OPEN trial recovers).
  Streaming is never retried mid-flight (a consumed stream cannot be
  replayed safely).
- **`AnthropicProvider`** implements the real `/v1/messages` protocol —
  system prompt as a top-level argument, `tool_use`/`tool_result`
  content blocks, `input_schema` tools, usage/stop-reason mapping — over
  the same `LLMProvider` contract. The bridge, gates, accounting, and
  capabilities changed ZERO lines to add a second real vendor.
- Config knobs: `WAX_LLM_RETRY_MAX_ATTEMPTS`, `WAX_LLM_BREAKER_FAILURE_THRESHOLD`,
  `WAX_LLM_BREAKER_RECOVERY_SECONDS`. Every vendor gets the identical
  resilience contract from configuration.

## Consequences

- Failure-mode behavior is a runtime property, not a per-adapter accident.
- Adding vendor #3 (Gemini, a local vLLM) is one adapter file + one
  selection branch — the architecture test for this is the existing
  contract suite, which now runs against wire-level mocks.
