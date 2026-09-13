"""Principal repository — data access for the identity system.

Repository pattern isolates the ORM choice. Callers go through repository
methods, never touch the Session directly. This is what makes the DB
replaceable (INV-07).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.runtime.logging import get_logger
from wax.state.identity_models import Principal, PrincipalCredential

log = get_logger(__name__)


def _new_ulid() -> str:
    """Generate a new ULID as a 26-char string."""
    return str(ULID())


class PrincipalRepository:
    """Data access for principals and their credentials.

    Each method takes an AsyncSession (caller-managed transaction). The
    repository does not commit — that's the caller's responsibility.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -----------------------------------------------------------------
    # Principal CRUD
    # -----------------------------------------------------------------

    async def create_principal(
        self,
        display_name: str | None = None,
    ) -> Principal:
        """Create a new principal. Returns the unsaved-but-attached object."""
        principal = Principal(
            id=_new_ulid(),
            status="active",
            display_name=display_name,
        )
        self._session.add(principal)
        await self._session.flush()
        log.info(
            "identity.principal.created",
            principal_id=principal.id,
            display_name=display_name,
        )
        return principal

    async def get_principal(self, principal_id: str) -> Principal | None:
        """Return a principal by ID, or None if not found.

        Soft-deleted principals are returned (so callers can distinguish
        "deleted" from "never existed"); callers should check `is_active`.
        """
        return await self._session.get(Principal, principal_id)

    async def soft_delete_principal(self, principal_id: str) -> bool:
        """Mark a principal as deleted (soft-delete). Returns True if found."""
        principal = await self.get_principal(principal_id)
        if principal is None:
            return False
        principal.deleted_at = datetime.now(UTC)
        principal.status = "deleted"
        await self._session.flush()
        log.info("identity.principal.soft_deleted", principal_id=principal_id)
        return True

    # -----------------------------------------------------------------
    # Credential management
    # -----------------------------------------------------------------

    async def add_credential(
        self,
        principal_id: str,
        kind: str,
        value: str,
        is_verified: bool = False,
    ) -> PrincipalCredential:
        """Attach a credential to a principal.

        Raises ValueError if (kind, value) already exists for another principal.
        Raises LookupError if the principal does not exist.
        """
        # Verify principal exists
        principal = await self.get_principal(principal_id)
        if principal is None:
            raise LookupError(f"Principal not found: {principal_id}")

        # Check (kind, value) uniqueness
        existing = await self._session.scalar(
            select(PrincipalCredential).where(
                PrincipalCredential.kind == kind,
                PrincipalCredential.value == value,
            )
        )
        if existing is not None:
            if existing.principal_id == principal_id:
                # Already attached to this principal — return as-is
                return existing
            raise ValueError(
                f"Credential ({kind}, ...) is already attached to another principal"
            )

        cred = PrincipalCredential(
            id=_new_ulid(),
            principal_id=principal_id,
            kind=kind,
            value=value,
            is_verified=is_verified,
        )
        self._session.add(cred)
        await self._session.flush()
        log.info(
            "identity.credential.added",
            principal_id=principal_id,
            credential_id=cred.id,
            kind=kind,
            is_verified=is_verified,
        )
        return cred

    async def find_credential(
        self, kind: str, value: str
    ) -> PrincipalCredential | None:
        """Look up a credential by (kind, value). Used at auth time."""
        result = await self._session.execute(
            select(PrincipalCredential).where(
                PrincipalCredential.kind == kind,
                PrincipalCredential.value == value,
            )
        )
        return result.scalar_one_or_none()

    async def resolve_principal_by_credential(
        self, kind: str, value: str
    ) -> Principal | None:
        """Find the principal associated with a (kind, value) credential.

        This is the core of interface-independent identity: an interface
        adapter (e.g. WhatsApp webhook) resolves its identifier to a
        universal principal via this method.
        """
        cred = await self.find_credential(kind, value)
        if cred is None:
            return None
        return await self.get_principal(cred.principal_id)

    async def mark_credential_used(self, credential_id: str) -> None:
        """Update last_used_at on a credential. Call after successful auth."""
        cred = await self._session.get(PrincipalCredential, credential_id)
        if cred is not None:
            cred.last_used_at = datetime.now(UTC)
            await self._session.flush()

    async def list_credentials(self, principal_id: str) -> list[PrincipalCredential]:
        """List all credentials attached to a principal."""
        result = await self._session.execute(
            select(PrincipalCredential).where(
                PrincipalCredential.principal_id == principal_id
            )
        )
        return list(result.scalars().all())
