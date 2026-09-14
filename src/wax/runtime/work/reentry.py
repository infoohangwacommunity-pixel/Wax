"""Durable intelligence re-entry — neutral contracts (ADR-0034).

These contracts live HERE (in `wax.runtime.work.reentry`) so that:

- the work handler (`wax.runtime.work.handlers.intelligence_handler`) can
  import them — it already imports from `wax.runtime.work.*`;
- the bridge (`wax.runtime.bridge.service.RuntimeBridge`) can import them
  — the bridge is allowed to import from `wax.runtime.work` for contracts;
- neither the work handler NOR the bridge imports the other.

The composition root (`wax.runtime.app.create_app`) wires the bridge's
`run_reentry` method as the callback on `RuntimeServices.reentry_callback`.

This is the universal mechanism that lets a long-running objective wake the
intelligence on a runtime fact (time or signal) — without the human having
to send another message. The runtime owns the wake; the intelligence
composes the response.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

# Bounded prompt size — the AI's instruction for re-entry must fit in a
# reasonable envelope (it becomes one of the messages in the LLM context).
# 4000 chars is roughly 1000 tokens, plenty for "reassess the objective
# after the user replied" or "continue when work X finishes".
REENTRY_PROMPT_MAX_CHARS = 4000

# Bounded observation size — the typed runtime evidence about the wake
# (event name, work_id, result dict). Smaller than the prompt because it
# is structured data, not natural language.
REENTRY_OBSERVATION_MAX_BYTES = 16_000

# The valid sources for an observation. "runtime" is the only one today;
# future sources (e.g. "connector", "approval") would extend this set
# deliberately — they change what the observation means.
REENTRY_OBSERVATION_SOURCES = frozenset({"runtime"})

ReentryOutcome = Literal["succeeded", "failed", "waiting", "awaiting_human"]


@dataclass(frozen=True)
class ReentryRequest:
    """Neutral request: the work handler's call to the bridge.

    All fields are runtime-owned facts — no model claims, no authority
    assertions, no secrets. The bridge constructs the LLM context from
    these + the existing continuity state.
    """

    principal_id: str
    originating_execution_id: str  # the parent execution that scheduled this work
    work_item_id: str  # the work item that woke
    prompt: str  # bounded intelligence instruction
    observation: dict[str, Any]  # typed runtime evidence about the wake


@dataclass(frozen=True)
class ReentryResult:
    """Neutral result: the bridge's reply to the work handler.

    The handler returns this to the work runner (which mark_succeeded
    the work item). The `outcome` field drives objective reconciliation
    in the work handler — NOT in the bridge.
    """

    execution_id: str  # the NEW continuation execution the bridge created
    objective_id: str
    conversation_id: str
    outcome: ReentryOutcome
    response_text: str | None = None
    error: str | None = None


# The callback type the composition root registers. Callable, not
# AbstractMethod — the bridge is the only implementer today; a generic
# ABC would add ceremony without value.
ReentryCallback = Callable[[ReentryRequest], Awaitable[ReentryResult]]


class ReentryValidationError(ValueError):
    """Raised when a re-entry work item's payload fails validation."""


def validate_reentry_payload(payload: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Validate the bounded payload shape for an `intelligence` work item.

    Returns (prompt, observation). Raises ReentryValidationError on any
    shape/size violation. This runs at SCHEDULE time (so the scheduler
    cannot write a malformed work item) and again at WAKE time (so a
    payload tampered with after scheduling is still rejected).

    The payload MUST NOT contain:
    - secrets / tokens (the validator does not regex-scan — the contract
      is that the bridge's intelligence context builder never reads these
      fields, and the work handler only passes the validated prompt +
      observation dict to the bridge);
    - host paths (the observation's `result` dict may contain arbitrary
      data, but the bridge presents it as a tool-message string, never as
      a file path the model can read);
    - authority claims (the observation's `source` must be "runtime";
      the intelligence cannot inject "user" or "system" sources).
    """
    if payload is None:
        raise ReentryValidationError("intelligence work payload is required")
    if not isinstance(payload, dict):
        raise ReentryValidationError("intelligence work payload must be an object")

    prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        raise ReentryValidationError("payload.prompt must be a string")
    if not prompt.strip():
        raise ReentryValidationError("payload.prompt must not be empty")
    if len(prompt) > REENTRY_PROMPT_MAX_CHARS:
        raise ReentryValidationError(
            f"payload.prompt exceeds {REENTRY_PROMPT_MAX_CHARS} chars"
        )

    observation = payload.get("observation")
    if not isinstance(observation, dict):
        raise ReentryValidationError("payload.observation must be an object")
    source = observation.get("source")
    if not isinstance(source, str):
        raise ReentryValidationError("payload.observation.source must be a string")
    if source not in REENTRY_OBSERVATION_SOURCES:
        raise ReentryValidationError(
            f"payload.observation.source must be one of {sorted(REENTRY_OBSERVATION_SOURCES)}"
        )
    event = observation.get("event")
    if not isinstance(event, str) or not event.strip():
        raise ReentryValidationError(
            "payload.observation.event must be a non-empty string"
        )

    # Size check on the whole observation (after serialization).
    import json

    try:
        obs_bytes = len(json.dumps(observation, default=str).encode("utf-8"))
    except (TypeError, ValueError) as e:
        raise ReentryValidationError(
            f"payload.observation is not JSON-serializable: {e}"
        ) from e
    if obs_bytes > REENTRY_OBSERVATION_MAX_BYTES:
        raise ReentryValidationError(
            f"payload.observation exceeds {REENTRY_OBSERVATION_MAX_BYTES} bytes "
            f"(was {obs_bytes})"
        )

    return prompt, observation
