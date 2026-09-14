# Interface Audit — Constitutional Audit

Scope: `src/wax/interfaces/`, `src/wax/runtime/bridge/`, canonical events, upward-leakage scan.

## Canonical event boundary (verified)

- Identity is principal-based: interface credentials (`whatsapp_phone`, `web_session`, …) ATTACH to a principal; first contact creates the principal and assigns the default role. The runtime never sees "a WhatsApp user" — it sees a principal arriving through an interface.
- Inbound: Meta payload → `WhatsAppIncomingMessage` (adapter, `interfaces/whatsapp/adapter.py`) → canonical `RuntimeRequest` — translation split between the adapter and `runtime/app.py` (composition root). Flagged: the canonical translation living in `runtime/app.py` is composition-root wiring, acceptable, but the adapter-owned variant would be cleaner.
- Outbound: `DeliveryRouter` (registry of senders) + `DeliveryQueue` (durable pending/retrying/delivered/failed with backoff + horizon) are interface-agnostic BY CONSTRUCTION. Delivery receipts are adapter-logged, not canonicalized — flagged as acceptable (no runtime decision consumes them).
- Interface↔credential mapping: was triplicated — **FIXED `47887cb`**: single source of truth in `wax.identity.contracts`.

## Upward-leakage scan (import-level, now machine-enforced)

Post-fix grep + the new `test_no_interface_coupling.py` (AST scan): `wax.interfaces` / `wax.runtime.bridge` are importable ONLY from the boundary itself and the composition root. Remaining string mentions outside the boundary are configuration field names (`whatsapp_access_token` etc. — config must name what it configures) and the credential-kind vocabulary — identity/config facts, not coupling.

The deepest leak — **Meta's 24-hour window enforced runtime-wide with a `"whatsapp"` default** — was CV-1, **FIXED `47887cb`**: vendor policy is now declared by the WhatsApp adapter at its wiring point via `DeliveryPolicy`; the runtime enforces whatever the attached interface declared, generically, and invents no vendor rule. A Telegram or API delivery is no longer subject to Meta's policy.

## Placeholder seams (honest)

`InterfaceKind.WEB/TELEGRAM/API` exist with no adapters (raise honest errors); only WhatsApp is registered. They document the seam without faking support. The media pipeline (real extractors, integration-tested) is built but unwired into the inbound path — its overclaiming comments were corrected; wiring is interface-multimodality work.

## Verdict

GOOD core, edges now clean. Principal identity, canonical RuntimeRequest/RuntimeResponse, and the durable delivery lifecycle are real and interface-agnostic; WhatsApp is genuinely below the runtime, and the boundary is enforced by a test, not by discipline.
