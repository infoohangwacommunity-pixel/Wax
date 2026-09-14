# ADR-0007: Interface Intelligence — The Interface Owns Its Wire Limits

**Status:** Accepted
**Date:** 2026-09
**Deciders:** WAX implementation agent, per roadmap Phase W

## Context

WhatsApp limits a text message to 4096 characters. The first implementation
handled this with a hard 4000-char cap in the **bridge**, which meant: (a)
long AI answers were rejected rather than delivered, and (b) a WhatsApp
constraint leaked into interface-agnostic runtime code. WhatsApp is our
first interface, not the architecture itself.

## Decision

- The **WhatsApp client** owns `send_long_text`: chunking at the wire
  limit, preferring paragraph/line boundaries, marking multi-part replies
  `(i/n)`, passing short messages through verbatim.
- The **runtime** keeps only an interface-agnostic response ceiling
  (12000 chars) expressed in the `message.send` capability schema; the
  interface decides how to honor it on its own wire.
- Interfaces attach to the runtime's delivery router; they never own it
  and never appear in `wax.core`.

## What this deliberately is NOT

- Not template logic, not interactive-message builders in the runtime,
  not Meta-specific retry semantics in the bridge. Those live in the
  interface package. We also do not assume Meta capabilities that are not
  documented in the Cloud API (e.g., free-form 24h-window rules are
  honored, never invented around).

## Consequences

- Adding a second interface (web, Telegram, voice) requires zero runtime
  changes: register a sender that honors the capability contract.
- Delivery failures surface as real errors into the durable-work
  dead-letter path (ADR-0004) — the AI learns the truth about its output.
