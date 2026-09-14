# Provider Audit — Constitutional Audit

Scope: `src/wax/intelligence/` (service, adapters, resilience, context_limits), provider assumptions in core.

## Model-independence verification

| Requirement | Reality | Class |
|---|---|---|
| OpenAI adapter | OpenAI-compatible via direct httpx (no SDK lock-in), sole `openai`/`tiktoken` import site (AST-test-enforced since `bcec2a5`) | LIVE |
| Anthropic adapter | Messages-API wire translation in-adapter; honest 4-chars/token estimator with provenance when no tokenizer | LIVE |
| Fallback chain | Ordered candidates from `WAX_LLM_PROVIDER_FALLBACKS`; per-candidate retry + circuit breaker; boot-time loud misconfiguration; duplicate skip; honest last-error raise; serving provider recorded in the response | LIVE (tested) |
| Retry / breaker | Error classification (408/409/429/5xx/transport transient; 4xx permanent); retry-inside, breaker-outside; per-provider metrics; generic `wax/reliability/` is provider-agnostic | LIVE |
| Context negotiation | Tokenizer-exact-when-available accounting behind the adapter duck type; runtime core is tokenizer-free; calibration clamp + provenance | LIVE |
| Token accounting | Exact in OpenAI adapter (tiktoken optional, failure cached); Anthropic uses declared limits; no hardcoded context size in core (`_DEFAULT_CONTEXT_LIMIT` lives IN the adapter) | LIVE |
| Stream behavior | `stream()` is primary-only — NO failover. Honest gap (streaming unused in production); class docstring overclaim corrected to state it | IMPLEMENTED-BUT-UNWIRED (documented) |
| Structured output / tool-calling | Internal contract (LLMRequest/LLMMessage/ToolSpec); provider-specific structures live only in adapters; unparseable tool arguments surfaced, not swallowed | LIVE, clean |

## Hidden provider assumptions found

1. Shared `llm_base_url` across both vendors — a mixed-vendor chain silently pointed Anthropic at an OpenAI-compatible proxy. **FIXED `bcec2a5`** (separate `anthropic_base_url`).
2. No Retry-After handling on 429s (fixed backoff schedule) — flagged, minor.
3. `ProviderKind.GOOGLE/MISTRAL/LOCAL` — honest placeholders raising "not implemented"; they document the seam without faking support.

## Verdict

GOOD. Two real contract-stable adapters, real classified retry + per-provider breakers, genuinely working tested failover, exact-where-possible token accounting with honest provenance where not. The boundary is now machine-enforced (INV-03 architecture tests), not review-enforced.
