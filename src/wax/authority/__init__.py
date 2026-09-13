"""wax.authority — runtime-enforced authorization.

The runtime is the sole authority on what an identity may do. The model
cannot grant itself authority by producing text. (Directive §7, §45, §147)

Architecture:
- `Permission` is a string-typed capability identifier (e.g. "memory.read",
  "capability.invoke:web_search", "execution.start").
- `Role` is a named bundle of permissions.
- `PrincipalRole` associates a principal with a role.
- `AuthorizationService` is the single point where "may this principal do
  X?" is answered. Every capability invocation, every state mutation, every
  sensitive action must pass through this service.

INVARIANT INV-04: AI-requested actions must pass through runtime authorization.
Enforced by: the capability invocation layer (Phase G) MUST call
AuthorizationService.check() before invoking any capability. An architecture
test will verify this.
"""

from wax.authority.models import PrincipalRole, Role
from wax.authority.service import AuthorizationService

__all__ = ["AuthorizationService", "PrincipalRole", "Role"]
