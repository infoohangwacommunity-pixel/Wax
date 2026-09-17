# WAX Memory

Memory is the intelligence's continuity across time. It is a runtime
subsystem, NOT a model-facing tool.

## Automatic pipeline

1. **Before the model runs**: the runtime retrieves relevant memories
   based on the user's message. The model sees them in the system prompt.
2. **After the interaction**: the runtime extracts memories from the
   exchange. The model does not have to call `memory.store`.
3. **Across time**: memories are consolidated, superseded, and forgotten
   by the runtime.

## Memory record

A memory record has:
- `content` — what the memory says
- `kind` — episodic, semantic, procedural, contextual, external
- `provenance` — where it came from (user_statement, model_observation, etc.)
- `confidence` — 0.0 to 1.0
- `importance` — 0.0 to 1.0
- `observed_at` — when it was observed
- `expires_at` — optional TTL
- `status` — active, superseded, forgotten
- `superseded_by` — forward link when a newer memory replaced this one

## Provenance is non-negotiable

Every memory is traceable:
- user statement > model inference > external observation
- The system never silently promotes an inference to a user fact.

## Forgetting is real

When the user says "forget that," the memory is soft-deleted (status =
forgotten). It is excluded from all retrieval paths. The row is retained
for audit but the user-facing semantic is: it is forgotten.
