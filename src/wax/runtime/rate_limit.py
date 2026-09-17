"""Simple in-memory rate limiter.

Per directive §24: rate limiting protects infrastructure, not intelligence.
This is NOT a cage. It prevents a single user from sending 10,000 messages
per second and crushing the server / infinite API bills.

30 lines. One dict. No database. No complex subsystem.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    """Per-principal, per-hour message rate limiter.

    In-memory only — resets on restart. That's fine for MVP. If we need
    multi-instance rate limiting later, we can move to Redis. But the
    directive says: simple, no bureaucracy.
    """

    max_messages_per_hour: int = 30
    window_seconds: float = 3600.0
    _hits: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))

    def check(self, principal_id: str) -> bool:
        """Returns True if the principal is within the rate limit."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        # Prune old hits
        self._hits[principal_id] = [t for t in self._hits[principal_id] if t > cutoff]
        if len(self._hits[principal_id]) >= self.max_messages_per_hour:
            return False
        self._hits[principal_id].append(now)
        return True

    def remaining(self, principal_id: str) -> int:
        """How many messages the principal has left in the current window."""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        current = len([t for t in self._hits[principal_id] if t > cutoff])
        return max(0, self.max_messages_per_hour - current)
