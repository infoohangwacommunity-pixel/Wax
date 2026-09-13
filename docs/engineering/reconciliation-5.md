# Reconciliation #5 — fifth principal-engineer pass (continuation loop)

Basis: repository at `b1a7c9e` → this pass advanced main through
`f9f1334`, `e08efa2`, `1a836c8`, `cd541f5`, `6a75039`, `8c8b72d`.
Start state verified: 518 tests passing, live probe 12 PASS exit 0,
local main == origin/main, clean tree.

The four boundaries recorded in reconciliation-4 as "deliberate
non-goals" were re-evaluated against the Universal Primitive Test.
ALL FOUR are infrastructure. All four are now implemented — the
previous pass's classification was honest about the contract but
wrong to leave the mechanisms unbuilt, because every one of them can
be provided in-process with zero new deployment dependencies.

## The four boundaries, closed

| # | Former non-goal | Verdict | Implementation + proof |
|---|---|---|---|
| 1 | Container-grade isolation for `code.run` | BUILD | `NamespaceBoundary` (ADR-0016): unprivileged user namespaces (user+mount+pid+net+ipc+uts) give kernel-enforced no-network, read-only filesystem (workspace re-bound rw at its original path), masked /proc+/sys (kills the same-uid `/proc/*/environ` secret channel), private size-capped noexec /tmp, rlimits (AS/NPROC/FSIZE/CPU), process-group kill. 20 adversarial tests + probe. Selection `isolation_backend=auto|namespace|subprocess` with LOUD metered fallback |
| 2 | Tokenizer-exact context accounting | BUILD | Adapters own their counters (ADR-0018): OpenAI loads real tiktoken per model family (optional `exact-tokens` extra, lazy, failure-safe); every adapter declares `token_counter` provenance; negotiation converts the token limit at the ADAPTER'S OWN calibrated ratio (clamped [2,6]); `ContextBudget` carries counter+ratio; `complete()` logs input tokens by the provider's own math. 23 tests |
| 3 | Maintenance-loop leader election | BUILD | Per-pass Postgres advisory-lock leadership (ADR-0017); SQLite = documented single_writer; unknown dialects fail OPEN; followers skip VISIBLY (result + log + `maintenance_leadership_total{role}` metric); work-claim concurrency already safe (SKIP LOCKED + fencing) and deliberately NOT elected |
| 4 | ADR-0010 retrieval upgrade | BUILD | Two-stage retrieval (ADR-0019): RECALL = newest-N ∪ lexical term matches (portable; PG additionally gets the generated tsvector + GIN from migration `d9e4f2a8b1c7`, dialect-guarded no-op on SQLite) then RANK = Okapi BM25 (k1=1.2, b=0.75) blended with recency+confidence. 11 property tests; migration chain fresh/upgrade/rollback green |

## Forensic gap hunt #5 — found and fixed

| Finding | Category | Fix |
|---|---|---|
| Ledger prune stats reported `ledger_total_after=-5` (live-probe evidence) | accounting bug | `total` is measured AFTER retention deletes; subtracting `retention_deleted` again double-counted. Fixed + regression test |
| Image OCR + PDF extraction were honest STUBS while `tesseract` and `pypdf` were installable | fake-capability gap | REAL extractors (ADR: reconciliation-5): `TesseractImageExtractor` (arg-vector subprocess, 20 MiB cap, group-kill timeout, self-gating) and `PdfDocumentExtractor` (pypdf text layer, honest `no_text_layer` for scans); verified end-to-end: OCR reads rendered text, PDF text layer extracted. Audio stays an HONEST stub (large model download = deployment decision) |
| `send_typing_indicator` posted a fabricated payload and commented itself as a placeholder; zero callers | fake implementation | REMOVED, with an in-place note explaining the removal and the bar for re-introducing it (live-verified Meta contract) |
| `PermissionNamespace` enum was dead aspirational code; role permissions could drift from declared permissions silently | dead code / drift | `BUILTIN_PERMISSIONS` now derives from the `_PERMISSION_MANIFEST` with an import-time namespace drift guard; new invariant tests: every role permission ∈ manifest; namespace grouping pinned |
| Dead symbols: `PrincipalCreate`, `PrincipalCredentialCreate`, `SendResult`, `services_from_app`, `utc_now`, `StubImageExtractor`, `StubDocumentExtractor` | dead code | removed (real extractors self-gate to identical honest behavior) |
| Sandbox rlimits were hardwired defaults | configurability | `isolation_memory_limit_mb` / `isolation_max_processes` / `isolation_max_file_bytes` settings wired through `code.run` |
| `NamespaceBoundary._probe` used `os.system` | hygiene | subprocess.run with arg-vector |
| ruff findings in src (unused imports, unsorted imports, quoted annotations, unused noqa) | hygiene | fixed |

## Test counts

- Session start (verified): 518
- After this pass: **581** (+63: namespace sandbox 20, leadership 9,
  token accounting 23 — plus context-budget/resilience additions,
  retrieval 11, extractor/media updates, ledger stats regression,
  authority consistency 2, minus removed obsolete stub assertions)
- Live probe: 12 PASS over real HTTP, exit 0 (re-verified after the
  isolation/leadership/tokenizer/retrieval changes)

## Re-evaluated non-goals (current, evidence-based)

- **MicroVM/Firecracker isolation** — the namespace sandbox covers the
  in-process threat model; a second VM layer requires a hypervisor
  deployment. The `IsolationBoundary` contract accepts one when a
  deployment needs it.
- **Audio transcription** — offline Whisper-class models are a
  multi-hundred-MB deployment decision; the stub is honest.
- **Embedding-based memory retrieval** — BM25 + lexical recall resolves
  the named failures at zero new infrastructure (ADR-0019 §non-goals).
- **Anthropic exact tokenizer** — no offline tokenizer exists; the
  count_tokens API must not sit in the budget hot path (ADR-0018).
