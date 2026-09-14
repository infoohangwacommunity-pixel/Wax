# ADR-0018: Tokenizer-exact context accounting inside adapters

Date: 2026-09-13
Status: Accepted
Supersedes the "real tokenizer in the runtime core" non-goal in
reconciliation-4.

## Context

The runtime's context-budget negotiation (ADR-0015) converts a
provider-advertised token limit into a CHARACTER budget for evidence
assembly using one fixed core constant: 4.0 chars/token. Two problems:

1. **Drift.** The conversion constant belongs to no one. A provider
   whose real tokenizer is denser or sparser than 4.0 gets a budget
   converted with the wrong math — silently.
2. **Estimation-only.** Adapters were permitted ("may expose") to count
   tokens, but none did. The `estimate_messages_tokens` helper used the
   portable estimator with no provider awareness and no consumer.

Re-evaluated against the Universal Primitive Test: exact token
accounting is infrastructure (any model, any interface, any workload).
The right home for a tokenizer is the ADAPTER (INV-03 — provider
knowledge stays in the provider file), not the runtime core.

## Decision

### 1. The adapter owns its counter

`OpenAIProvider` loads the real tiktoken encoding for its model family
(o200k_base for gpt-4o/o1/o3/o4, cl100k_base for gpt-4/gpt-3.5) lazily
and caches it per process. tiktoken is an OPTIONAL dependency
(`pip install wax[exact-tokens]`): deployments without it get the
documented estimator, deployments with it get EXACT counts — same code
path, no API round-trips, offline.

Failure semantics: a failed load (not installed, encoding files
unfetchable) is cached for the process and the adapter degrades to the
estimator. Every adapter now DECLARES its counter's provenance via
`token_counter` (e.g. `"tiktoken:o200k_base"`, `"estimate:4chars"`) —
the runtime never guesses what math it got.

`AnthropicProvider` deliberately keeps the estimator: Anthropic
publishes no offline tokenizer, and the count_tokens API is a network
round-trip that must not sit in the budget hot path. Its provenance
string says so honestly.

### 2. The negotiation calibrates to the adapter

`derive_context_budget` no longer converts the token limit with a core
constant. It asks the adapter to count a fixed calibration sample and
derives THE ADAPTER'S OWN chars/token ratio (`calibrate_chars_per_token`),
then converts the limit at that ratio. When the adapter is tiktoken-backed,
the char budget is consistent with the exact counter — no cross-tokenizer
drift.

The calibration is clamped to [2.0, 6.0] chars/token: a pathological
adapter cannot starve the evidence budget (claiming implausibly dense
tokens) or overflow the window (under-counting). A broken counter
degrades to the portable 4.0. Errors stay on the safe side by
construction.

### 3. Provenance end to end

- `ContextBudget` carries `counter` and `chars_per_token` — recorded in
  the `context.budget` log line next to the source and limit.
- `intelligence.complete.start` logs `estimated_input_tokens` computed by
  the provider's OWN counter (`provider_estimate_messages_tokens`) —
  the request's token cost by the same math the budget was built with.
- The previously dead `estimate_messages_tokens` helper gains a
  provider-aware sibling with a real consumer.

## Consequences

- Token accounting is exact where the ecosystem allows it and honestly
  labeled where it cannot be (Anthropic).
- The budget's conversion constant is the adapter's responsibility and
  its provenance is observable — a wrong ratio is a debugging fact, not
  a silent assumption.
- One optional dependency (`tiktoken`) behind an extra; its absence
  changes efficiency, never correctness (the estimator errs safe).
