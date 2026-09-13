"""wax.identity — interface-independent identity for WAX.

The identity system is the foundation of authorization. WAX identity must
NOT be tied to any single interface (WhatsApp phone number, email, etc.).
The same underlying continuity must survive interface replacement.

Architecture:
- `Principal` is the universal identity concept (a human or a service that
  can be authorized to do things).
- `PrincipalCredential` is a credential attached to a principal (phone,
  email, OAuth subject, etc.). A principal may have multiple credentials.
- `IdentityMapping` maps an interface-specific identifier (e.g., WhatsApp
  phone number) to a principal. Removing WhatsApp does not destroy the
  principal — only the mapping is lost.
"""

from wax.identity.models import Principal, PrincipalCredential
from wax.identity.repository import PrincipalRepository

__all__ = [
    "Principal",
    "PrincipalCredential",
    "PrincipalRepository",
]
