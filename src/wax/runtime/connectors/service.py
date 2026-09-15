"""Generic Connector Runtime — discovery + resolution (ADR-0041, Phase 8).

The runtime understands resource types (git_host, package_registry,
cloud_deployment, file_storage, messaging), NOT brands. The
intelligence discovers actual services (GitHub, GitLab, Codeberg,
npm, PyPI, Railway, Fly.io, Google Drive, Dropbox) through the
environment — no architectural change when a new platform appears.

Two capabilities:
- connector.discover: list available connector definitions + scopes
- connector.resolve: resolve a grant handle to a service binding

The intelligence NEVER sees raw secrets, host paths, or the vault's
encryption key.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.runtime.logging import get_logger
from wax.state.credential_models import (
    ConnectorDefinitionRecord,
    CredentialGrantRecord,
    PrincipalConnectionRecord,
)

log = get_logger(__name__)


# P0-Taxonomy: The core runtime does NOT know which brand is behind a
# connector. Brand discovery is the adapter's job, not the core's.
# The service_kind returned by resolve() is "unknown" unless an
# optional adapter provides the brand information.
_DEFAULT_SERVICE_KINDS: dict[str, str] = {}


class ConnectorRuntime:
    """The connector discovery + resolution service.

    All methods take the caller's session; transactions belong to the
    caller. The runtime NEVER returns secret values — only opaque IDs
    and handles.
    """

    def __init__(self, settings: Any | None = None) -> None:
        self._settings = settings

    async def discover(
        self,
        session: AsyncSession,
        *,
        connector_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """List available connector definitions + their supported scopes."""
        stmt = select(ConnectorDefinitionRecord)
        if connector_filter:
            stmt = stmt.where(ConnectorDefinitionRecord.name == connector_filter)
        records = (await session.execute(stmt)).scalars().all()
        return [
            {
                "name": r.name,
                "description": r.description,
                "supported_scopes": r.supported_scopes,
                "auth_methods": r.auth_methods,
                "version": r.version,
            }
            for r in records
        ]

    async def resolve(
        self,
        session: AsyncSession,
        *,
        grant_handle: str,
        principal_id: str,
    ) -> dict[str, Any]:
        """Resolve a grant handle to a service binding.

        Returns {binding_handle, service_kind, available_operations}.
        The binding_handle is opaque; the intelligence uses it to
        reference this binding. The service_kind is the discovered
        brand (github, gitlab, etc.) — learned through the environment,
        NOT hardcoded in the architecture.
        """
        grant = (
            await session.execute(
                select(CredentialGrantRecord).where(CredentialGrantRecord.handle == grant_handle)
            )
        ).scalar_one_or_none()
        if grant is None:
            raise ValueError("Invalid grant handle")
        if grant.principal_id != principal_id:
            raise ValueError("grant belongs to a different principal")
        if grant.status != "active":
            raise ValueError(f"grant is {grant.status}")

        conn = await session.get(PrincipalConnectionRecord, grant.connection_id)
        if conn is None or conn.status != "active":
            raise ValueError("connection is not active")

        # Discover the service kind from the default map.
        # A future cycle will add real brand detection via the secret's
        # structure (e.g. github tokens start with "ghp_") or a
        # per-principal config map.
        service_kind = _DEFAULT_SERVICE_KINDS.get(conn.connector_name, "unknown")

        # The binding handle is a deterministic derivation of the grant
        # ID + service kind — so the same grant always resolves to the
        # same binding (idempotent discovery).
        binding_handle = f"binding-{grant.id}-{service_kind}"

        log.info(
            "connector.resolved",
            principal_id=principal_id,
            connector_name=conn.connector_name,
            service_kind=service_kind,
            binding_handle=binding_handle,
        )

        return {
            "binding_handle": binding_handle,
            "service_kind": service_kind,
            "connector": conn.connector_name,
            "scopes": grant.scopes,
            "available_operations": self._available_operations(conn.connector_name, grant.scopes),
        }

    def _available_operations(self, connector_name: str, scopes: list[str]) -> list[str]:
        """Return the operations available for this connector + scopes.

        P0-Taxonomy: The core runtime does NOT hardcode operations per
        connector type. Operations are discovered through the environment
        or declared by optional adapters. The intelligence learns what
        it can do by trying, not by reading a hardcoded map.
        """
        return []
