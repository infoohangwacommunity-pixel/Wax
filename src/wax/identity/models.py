"""Re-export the persistence models so callers can import from wax.identity."""

from wax.state.identity_models import Principal, PrincipalCredential

__all__ = ["Principal", "PrincipalCredential"]
