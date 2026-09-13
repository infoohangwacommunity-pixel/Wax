"""Cost protection — caps LLM token spend per principal.

Without cost caps, a single user (or a prompt-injection attack that
triggers many LLM calls) could spend unlimited money.

Architecture:
- CostProtector: tracks per-principal token usage per day
- CostLimitConfig: daily token cap (default 100k tokens/day)
- check_and_record(usage_tokens): atomically checks + records; returns
  True if allowed, False if would exceed cap

INVARIANT: Cost caps are enforced by the runtime, never by the model.
The model cannot grant itself more tokens by producing text.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from threading import Lock
from typing import Any

from wax.runtime.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CostLimitConfig:
    """Cost protection configuration.

    Defaults: 100k tokens/day per principal (roughly $0.30-$1.50/day
    depending on model).
    """

    daily_token_cap: int = 100_000
    daily_message_cap: int = 200


class CostProtector:
    """Per-principal daily cost tracker + cap enforcer.

    In-memory; for production, persist to DB with daily reset.
    """

    def __init__(self, config: CostLimitConfig | None = None) -> None:
        self._config = config or CostLimitConfig()
        self._usage: dict[str, dict[str, int]] = {}  # principal_id → {date_str: tokens}
        self._messages: dict[str, dict[str, int]] = {}  # principal_id → {date_str: count}
        self._lock = Lock()

    def check_and_record(
        self,
        principal_id: str,
        *,
        tokens: int = 0,
        messages: int = 1,
    ) -> bool:
        """Atomically check + record usage.

        Returns True if the usage is within limits (and records it), False
        if it would exceed a cap (and does NOT record it).
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            tokens_used = self._usage.get(principal_id, {}).get(today, 0)
            msgs_used = self._messages.get(principal_id, {}).get(today, 0)

            if tokens_used + tokens > self._config.daily_token_cap:
                log.warning(
                    "cost.token_cap_exceeded",
                    principal_id=principal_id,
                    used=tokens_used,
                    requested=tokens,
                    cap=self._config.daily_token_cap,
                )
                return False

            if msgs_used + messages > self._config.daily_message_cap:
                log.warning(
                    "cost.message_cap_exceeded",
                    principal_id=principal_id,
                    used=msgs_used,
                    requested=messages,
                    cap=self._config.daily_message_cap,
                )
                return False

            # Record usage
            if principal_id not in self._usage:
                self._usage[principal_id] = {}
            self._usage[principal_id][today] = tokens_used + tokens

            if principal_id not in self._messages:
                self._messages[principal_id] = {}
            self._messages[principal_id][today] = msgs_used + messages

            return True

    def get_usage(self, principal_id: str) -> dict[str, dict[str, int]]:
        """Return current usage for a principal."""
        with self._lock:
            return {
                "tokens": dict(self._usage.get(principal_id, {})),
                "messages": dict(self._messages.get(principal_id, {})),
            }

    def reset_principal(self, principal_id: str) -> None:
        """Clear all usage for a principal (manual override)."""
        with self._lock:
            self._usage.pop(principal_id, None)
            self._messages.pop(principal_id, None)
