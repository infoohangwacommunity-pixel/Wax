# Database Audit — Constitutional Audit

Scope: all 19 tables in `src/wax/state/`, the 11-migration chain, config, per-table purpose interrogation.

## Table inventory (name | purpose | universal? | writer | reader | class)

| Table | Purpose | Universal | Writer | Reader | Class |
|---|---|---|---|---|---|
| principals | identity root | yes | bridge first-contact | authority, bridge | LIVE |
| principal_credentials | interface identifiers | yes | identity repo | bridge, app, delivery | LIVE |
| roles / principal_roles | permission bundles | yes | seed at boot | authorization | LIVE |
| audit_events | append-only security audit | yes | ubiquitous `record_audit_event` | nothing queries it | LIVE (write-only — flagged) |
| memory_records | per-principal memory | yes | memory repo | retrieval, expiry worker | LIVE |
| memory_links | typed evidence edges | yes | memory repo | search expansion | LIVE |
| executions | durable execution record | yes | execution repo (bridge/runner) | recovery scan, continuity | LIVE |
| execution_steps | checkpoint steps | yes | bridge (2 sites) | NOTHING (resume unwired) | IMPLEMENTED-BUT-UNWIRED |
| objectives | what the human wants | yes | bridge/work paths | transition map, continuity | LIVE |
| objective_executions | append-only participation history | yes | bridge/handlers/evidence | evidence.py, resume | LIVE |
| conversations | context grouping | yes | conversation service | continuity | LIVE (lifecycle partially dead) |
| processed_messages | inbound idempotency ledger | yes | bridge | dedup + recovery scan | LIVE (grows forever) |
| work_items | durable work + leases | yes | work repo | runner claim query | LIVE |
| runtime_signals | event ledger | yes | signal repo | claim EXISTS, retention | LIVE (only pruned table) |
| pending_approvals | human approval primitive | yes | approval service | bridge/handlers consume | LIVE (create-race documented → CV-11) |
| delivery_records | recoverable outbound sends | yes | delivery queue | maintenance retry | LIVE (1 of 4 declared sources wired → CV-13) |
| provisioned_resources | TTL resources | yes | provisioning | TTL reaper | LIVE (`limits` write-only) |
| artifacts | durable outputs w/ integrity | yes | workspace.acquire | continuity ARTIFACTS | LIVE (`metadata_json` write-only) |
| dead_letter_entries | terminal failures | yes | runner + bridge | TEST-ONLY inspection | LIVE (write-only — flagged) |

## Findings

1. **Retention asymmetry** (founder question #2): only `runtime_signals` prunes (waiter-safe). `processed_messages` (2k+5k chars/message of verbatim conversation), `audit_events`, `execution_steps`, `conversations`, `dead_letter_entries`, and terminal work/delivery/objective-execution rows grow forever.
2. **Duplicate indexes** baked into the root migration (`index=True` AND `__table_args__` on the same columns — executions, objectives, audit_events, dead_letter, conversations ×3, memory ×3). Clutter, harmless, documented; collapsing them is a schema-hygiene migration.
3. **Duplicated meaning**: `executions.objective` (Text) vs `objectives.description`; `processed_messages.request_text/response_text` vs the episodic memory written moments later; `conversations.last_execution_id` vs `objective_executions`. Flagged; collapsing requires careful migration, low urgency.
4. **Soft references**: no FKs on several principal/execution pointers (work_items, approvals, signals, dead letters, provisioning, memory supersession, artifacts, conversations, objective_executions). All READ paths are principal-scoped (verified across work.list, approvals, provisioning, memory); no cross-tenant leak found. `memory_links.unlink` is principal-unscoped at repo level — currently unexposed; safe by omission only.
5. **Concurrency**: `processed_messages` has a real unique constraint (the model to follow); `pending_approvals.create_or_get_pending` does not (→ CV-11, partial unique index designed). Leases use `FOR UPDATE SKIP LOCKED` + fencing — correct.
6. **SQLite/PG divergence**: handled honestly — `with_for_update(skip_locked)` ignored-on-SQLite documented; tsvector migration dialect-guarded; production hardening forbids SQLite. Naive/aware datetime repair is sprinkled (one dead_letter naive write FIXED to UTC `bcec2a5`).
7. **Migration discipline**: all 11 migrations have real downgrades; no data-destructive ops; chain head `e1a3c5e7b9d2`; fresh-DB + upgrade-from-production tests in CI.
8. **Doc bug**: `audit_models.py:59` claims JSONB-on-PostgreSQL — plain JSON actually. Corrected next docs pass (comment-only).

## Verdict

Constitutionally sound: every table is mechanism-shaped, principal-scoped on every read path, no education ontology anywhere in state. The wounds are operational (retention regime, duplicate indexes, one idempotency gap) — constraint additions and hygiene migrations, not redesigns.
