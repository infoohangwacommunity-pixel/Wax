"""wax.security — runtime security: prompt injection, abuse, rate limits, costs.

Phase W extends the Phase N/O security baseline with:
- AbuseDetector: detects suspicious patterns (message flood, repeated
  injection attempts, suspicious senders)
- RateLimiter: per-principal + per-IP request rate limiting
- CostProtector: caps LLM token spend per principal per day
- MaliciousMediaDetector: flags media that exceeds size limits or has
  suspicious mime types

INVARIANTS:
- Security is enforced by the runtime, NEVER by the model (Directive §46)
- Rate limits prevent abuse without locking out legitimate users
- Cost caps prevent runaway spend (Directive §114, §115)
"""

from wax.security.abuse import AbuseDetector, AbuseVerdict
from wax.security.cost_protection import CostLimitConfig, CostProtector
from wax.security.rate_limiter import (
    RateLimitConfig,
    RateLimitDecision,
    RateLimiter,
)

__all__ = [
    "AbuseDetector",
    "AbuseVerdict",
    "CostLimitConfig",
    "CostProtector",
    "RateLimitConfig",
    "RateLimitDecision",
    "RateLimiter",
]
