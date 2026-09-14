# WAX Security Model (OMEGA)

What is enforced, where, and what is honestly deferred. Verified at
`b439aae`. Companion to ADR-0002/0004/0007, ADR-0016, ADR-0027, and
`docs/constitutional-audit/interface-audit.md`.

## The core law

LAW 2: the model is not the security boundary. Prompts carry state;
they authorize nothing. Every guarantee below is enforced by runtime
code or database constraints, and each has a test or a live-probe
check behind it.

## Identity and ownership

- Principals are resolved from interface credentials by the runtime;
  the model is never told who it is acting as — it reads the principal
  id from context as an environment fact.
- Memory, objectives, work, artifacts, approvals: per-principal at the
  QUERY level (`WHERE principal_id = ...`), plus ownership checks in
  the capability layer, plus `ctx.principal_id` set by the invoker from
  the runtime-resolved principal (never model-supplied). Cross-principal
  access attempts are tested and refused (memory, links, work,
  objectives, workspaces, messaging).
- `message.send` can only reach the CALLING principal's own verified
  credentials — the AI cannot message third parties.
- The `ai` role is intentionally empty: the model has no permissions of
  its own; it acts through the requesting principal's authority.

## The enforcement points

1. **Security gate** (per message): token-bucket rate limit → daily
   cost caps → abuse/flood/injection heuristics → input sanitizer wraps
   untrusted content as marked data.
2. **Agency gate** (per action): the runtime's policy maps decision
   kinds to approval levels; externally-visible / financially-
   consequential / destructive / irreversible actions require a human.
3. **Approval primitive**: pending approvals are created atomically
   (partial unique index — CV-11), expire, bind to the exact request
   fingerprint, and are consumed EXACTLY ONCE via conditional UPDATE
   (CV-2). Decisions authenticate the HUMAN through their interface
   credential; there is no approve capability. Background-created
   approvals reach the human (CV-14) or become durable retry state.
4. **Authority service**: DB-backed role→permission with wildcard
   matching, checked inside the invoker for every invocation; every
   decision audited.
5. **Invoker**: the sole effect point — declared-input validation,
   timeout, structured honest failure taxonomy; output validation is a
   recorded gap (ADR-0026).
6. **Isolation**: `code.run` executes under a user-namespace sandbox
   (no network, read-only root, masked /proc+/sys, private noexec /tmp,
   rlimits, process-group SIGKILL) with LOUD fallback to subprocess
   (honestly documented as accident-isolation, not adversary-isolation).
   Container/microVM/browser kinds raise "not implemented" — no fake
   security (mission §74).
7. **Network egress**: SSRF guard for HTTP capabilities — scheme
   allowlist, DNS-resolved IP classification with anti-rebinding and
   connection pinning, per-hop redirect re-validation, byte caps.
8. **Webhook integrity**: HMAC signature verification (live-probe
   tested, including the bad-signature path).
9. **Credential isolation**: provider/interface tokens live in adapter
   headers built from settings; the model never receives raw
   credentials. Log redaction strips secret-shaped keys/values from
   every structured log event.

## Prompt-injection defense (mission §48)

Instruction hierarchy by construction: the stable runtime contract is
one system prompt; evidence arrives as separate labelled SYSTEM lines
(runtime-assembled, not model-echoed); untrusted content (user text,
retrieved documents, tool outputs) is sanitizer-marked so the model can
treat it as data. Enforcement never relies on the prompt: even a fully
compromised model output can only REQUEST capabilities, and every
request passes the gates above. Retrieval-poisoning scenarios are in
the evaluation suite.

## Honest boundaries (deferred, not faked)

- The security perimeter (rate/cost/abuse) is in-memory and
  single-instance; multi-instance deployment multiplies the limits —
  ADR-0027 documents the boundary and the designed shared store.
- `audit_events` "NEVER captures secrets" is a writer convention, not
  an enforced check (marked future in the model file).
- Metrics carry no principal dimension — deliberate, documented in
  `observability/__init__.py`.
- MicroVM-grade isolation is a declared enum with no implementation;
  the namespace sandbox states its own limits honestly.
- sensitivity/privacy enforcement awaits the founder privacy policy
  (ADR-0025).
