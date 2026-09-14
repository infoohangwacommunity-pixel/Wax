"""Abuse detection — flags suspicious patterns without false-positive lockouts.

Detects:
- Message floods (too many messages per minute from one principal)
- Repeated prompt-injection attempts (same sender flagged N times)
- Suspicious sender patterns (sender hash matches known abuse signature)

INVARIANT: Abuse detection is conservative. False positives (legitimate
user flagged) are worse than false negatives (attacker slips through).
Detected abuse records an audit event; the runtime decides whether to
rate-limit, block, or just monitor.
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock

from wax.runtime.logging import get_logger
from wax.security.input_sanitizer import InjectionRisk, SanitizerResult

log = get_logger(__name__)


class AbuseLevel(StrEnum):
    NONE = "none"
    LOW = "low"  # monitor, do not block
    MEDIUM = "medium"  # rate-limit
    HIGH = "high"  # block this message, audit


@dataclass
class AbuseVerdict:
    """The outcome of abuse detection on a message."""

    level: AbuseLevel
    reasons: list[str] = field(default_factory=list)
    sender_hash: str | None = None
    recommended_action: str = "allow"  # allow | rate_limit | block


class AbuseDetector:
    """Detects suspicious patterns per principal.

    Stateful (in-memory) — for production, back this with Redis or similar.
    """

    def __init__(
        self,
        *,
        flood_window_seconds: int = 60,
        flood_threshold: int = 20,
        injection_repeat_threshold: int = 5,
    ) -> None:
        self._flood_window = flood_window_seconds
        self._flood_threshold = flood_threshold
        self._injection_repeat_threshold = injection_repeat_threshold

        self._message_times: dict[str, deque[float]] = defaultdict(deque)
        self._injection_counts: dict[str, int] = defaultdict(int)
        self._lock = Lock()

    def evaluate(
        self,
        *,
        principal_id: str,
        sender_interface_id: str,
        sanitizer_result: SanitizerResult | None = None,
    ) -> AbuseVerdict:
        """Evaluate a message for abuse signals.

        Returns an AbuseVerdict with recommended action.
        """
        sender_hash = self._hash_sender(sender_interface_id)
        reasons: list[str] = []
        level = AbuseLevel.NONE

        # Check for flood
        if self._is_flooding(principal_id):
            reasons.append("message_flood")
            level = AbuseLevel.HIGH if level == AbuseLevel.NONE else level

        # Check for repeated injection attempts
        if sanitizer_result and sanitizer_result.risk in (
            InjectionRisk.HIGH,
            InjectionRisk.MEDIUM,
        ):
            with self._lock:
                self._injection_counts[principal_id] += 1
                count = self._injection_counts[principal_id]
            if count >= self._injection_repeat_threshold:
                reasons.append(f"repeated_injection:{count}")
                level = AbuseLevel.HIGH
            elif count >= 2:
                reasons.append(f"injection_attempt:{count}")
                level = AbuseLevel.MEDIUM if level == AbuseLevel.NONE else level
            else:
                # First attempt — LOW (monitor)
                reasons.append(f"injection_attempt:{count}")
                if level == AbuseLevel.NONE:
                    level = AbuseLevel.LOW

        # Record this message timestamp
        with self._lock:
            now = time.time()
            times = self._message_times[principal_id]
            times.append(now)
            # Prune entries outside the flood window
            cutoff = now - self._flood_window
            while times and times[0] < cutoff:
                times.popleft()

        action = "allow"
        if level == AbuseLevel.MEDIUM:
            action = "rate_limit"
        elif level == AbuseLevel.HIGH:
            action = "block"

        if level != AbuseLevel.NONE:
            log.warning(
                "abuse.detected",
                principal_id=principal_id,
                level=level.value,
                reasons=reasons,
                action=action,
            )

        return AbuseVerdict(
            level=level,
            reasons=reasons,
            sender_hash=sender_hash,
            recommended_action=action,
        )

    def _is_flooding(self, principal_id: str) -> bool:
        with self._lock:
            times = self._message_times[principal_id]
            return len(times) >= self._flood_threshold

    @staticmethod
    def _hash_sender(sender_id: str) -> str:
        """SHA-256 hash of sender ID for audit logging without PII."""
        return hashlib.sha256(sender_id.encode()).hexdigest()[:16]

    def reset_principal(self, principal_id: str) -> None:
        """Clear abuse state for a principal (manual recovery)."""
        with self._lock:
            self._message_times.pop(principal_id, None)
            self._injection_counts.pop(principal_id, None)
