"""wax.security — runtime security: trust boundaries, prompt-injection defense.

Security lives outside prompts (Directive §46, §141). The runtime enforces
trust boundaries; prompts cannot grant authority.

Architecture:
- TrustBoundary: which actors are trusted at what level
- InputSanitizer: detects + neutralizes prompt-injection patterns
- CapabilityGuard: enforces that capability outputs are treated as data,
  never as instructions (confused deputy defense)

INVARIANT INV-04: AI-requested actions pass through runtime authorization.
INVARIANT: External input (web content, tool output, files) is UNTRUSTED.
"""

from wax.security.input_sanitizer import InputSanitizer, SanitizerResult
from wax.security.trust import TrustBoundary, TrustLevel

__all__ = [
    "TrustBoundary",
    "TrustLevel",
    "InputSanitizer",
    "SanitizerResult",
]
