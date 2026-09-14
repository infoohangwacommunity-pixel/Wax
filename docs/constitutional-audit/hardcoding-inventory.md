# Hardcoding Inventory — Constitutional Audit

Every hardcoded decision found, classified. Hardcoding that is a *mechanism constant* (documented, provider/interface-independent, no domain vocabulary) is constitutionally acceptable; the violations are flagged.

## 1. Domain/vendor-policy hardcoding (CONSTITUTIONAL VIOLATION class — remediated)

| Item | Pre-fix location | Class now |
|---|---|---|
| Meta 24h customer-service window applied to all interfaces | `runtime_capabilities.py:44` | **FIXED `47887cb`** — now a WhatsApp-adapter-declared `DeliveryPolicy` |
| `"whatsapp"` default interface in `message.send` schema+impl | `runtime_capabilities.py:248,559` | **FIXED `47887cb`** — interface derived from principal credentials |
| Interface↔credential mapping triplicated (bridge ×2, capabilities) | `bridge/service.py:92,870`, `runtime_capabilities.py:49` | **FIXED `47887cb`** — single source of truth in `wax.identity.contracts` |
| Shared `llm_base_url` across OpenAI-compatible AND Anthropic providers | `intelligence/service.py:103,122,205,219` | **FIXED `bcec2a5`** — separate `anthropic_base_url` |
| Hardcoded fallback model names `gpt-4o-mini`, `claude-3-5-haiku-latest` | `intelligence/service.py:104,206,220` | LIVE (documented defaults; config-overridable) — acceptable |

## 2. Mechanism constants (LIVE, acceptable — no domain vocabulary)

- Work: `MAX_WORK_DELAY=30d`; poll 2s; lease 120s; reclaim backoff 30s; batch 10; stale-execution 900s; max attempts 1–10 per descriptor.
- Delivery: backoff base 60s ×2.0; attempts 5; deliverability horizon 24h (interface-agnostic setting; comment de-Meta'd).
- Execution: code 20k chars / 30s timeout / 35s wrapper; http.get body cut 10k; fetch cap 1MB/30s; invoker per-capability timeouts.
- Budget: `_DEFAULT_EXECUTION_BUDGET` 300s/100k tok/8 calls/10 invocations; tool-result cap 8000 chars; response cap 3500 chars.
- Memory: BM25 k1=1.2 b=0.75; blend 0.7/0.3 recency + 0.1 confidence + 0.1 importance; decay τ=14d; candidate pool 200; top-8 terms; expansion top-3 anchors ×≤5 neighbors damped 0.6.
- Context: priorities 0–5; `_MIN_TAIL=200`; `CHARS_PER_TOKEN=4.0` clamp [2.0,6.0]; `MIN_BUDGET_CHARS=1200`; pools 5/5→8.
- Approvals: expiry 86400s default (now config-driven end-to-end); summary truncation 120/8.
- Provisioning: MAX_TTL 24h.
- Scheduling philosophy: **clean** — zero timer/reminder/countdown/study-session code in runtime (grep-verified; `work_models.py:19-22` documents that reminders are a composition of work+waiting+signals+delivery).

## 3. Dead / unwired / placeholder inventory (with dispositions)

| Item | Location | Class | Disposition |
|---|---|---|---|
| Postgres FTS recall arm (tsvector column + GIN index, no callers; docstring cites a nonexistent parameter) | migration `d9e4f2a8b1c7`, `memory/repository.py:482-483` | DOCUMENTATION-ONLY | Next cycle: wire as an optional PG recall arm or drop the migration + ADR-0019 claim. Do not leave silently. |
| Conversation `set_summary` / `archive_stale` / `close` lifecycle | `continuity/repository.py:99-161` | IMPLEMENTED-BUT-UNWIRED | Wire into maintenance (idle→idle/archived) when conversation lifecycle work is scheduled; conversations currently live forever — honest but flagged |
| Execution checkpoint resume (`update_checkpoint`, `get_latest_checkpoint`, `list_steps`) | `execution/repository.py:174-256` | IMPLEMENTED-BUT-UNWIRED | Checkpoints are written every execution; the resume reader is the missing half. Next-cycle wiring target for objective resumption (mission §99). `execution/__init__.py` overclaim corrected to honest recovery-only language |
| `count_open_executions` | `objective/repository.py:264-271` | TEST-ONLY | Candidate for removal or for use by the outstanding-work guard |
| `ResourceAccountant.release()` | `resources/accountant.py:126` | DEAD | Allocations accumulate for process lifetime. Wire release at execution finalize (small fix, next batch) |
| `ExecutionKind.AGENT_LOOP/LONG_RUNNING_TASK/BACKGROUND_WORKFLOW` | `execution/contracts.py:37-39` | PLACEHOLDER | Only `SINGLE_TURN` is created; keep until multi-execution objectives land, then use or delete |
| Agency `EXTERNALLY_VISIBLE` tier (`SEND_MESSAGE`/`CALL_EXTERNAL_API`) | `agency/contracts.py:97-98` | IMPLEMENTED-BUT-UNWIRED | No dispatch path constructs these decision kinds; message-send safety currently rests on the ownership check. Decide: wire the tier into the gate or remove the constants |
| `InterfaceKind.WEB/TELEGRAM/API`, `ProviderKind.GOOGLE/MISTRAL/LOCAL` | `bridge/contracts.py`, `intelligence/contracts.py` | PLACEHOLDER | Honest placeholders (raise "not implemented"); they document the adapter seams without faking them |
| Media pipeline (real Tesseract/pypdf extractors, integration-tested) | `media/pipeline.py`, `app.py:432-439` | IMPLEMENTED-BUT-UNWIRED | Overclaiming comments corrected; wiring decision belongs to interface multimodality work |
| Dead-letter inspection (`list_recent`, `mark_reprocessed`) | `reliability/dead_letter.py` | TEST-ONLY | Dead letters are written, never re-driven — flag for a maintenance sweep |
| `MemoryRead.from_orm`, `MemoryProvenance` enum, `archived` memory status | `memory/` | DEAD | Harmless; removal candidates in the next cleanup batch |
| `processed_messages` outcome docstring listing an outdated state set | `state/bridge_models.py:53` | DOCUMENTATION-ONLY | Comment corrected to match `_TERMINAL_OUTCOMES` |
| `analysis.txt` stray probe output at repo root | repo root | DEAD | Removed in the docs commit |

## 4. Runtime "how to think" scan (agentic audit)

No forced planning, no workflow prescriptions, no hardcoded next-step suggestions found. Capability descriptions state contracts and honest constraints ("the runtime cannot send messages right now"), never strategies. The two borderline coaching texts (approval structured-failure guidance; "use this when the message belongs to earlier work" trigger hints) describe real runtime affordances — mechanism-honest, retained.
