"""Credential Vault — provider-neutral secret storage (ADR-0040, Phase 7).

The vault NEVER exposes secrets to the model. It stores secrets
encrypted at rest, issues opaque handles, and injects secrets only
into approved environment boundaries (subprocess env vars of
terminal sessions — never the model's prompt).

The vault understands resource types (git_host, package_registry,
cloud_deployment, file_storage, messaging), NOT brands. Adding a new
connector type is a declarative act (insert a connector_definition
row), not a code change.
"""

from __future__ import annotations

import secrets as pysecrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.runtime.logging import get_logger
from wax.runtime.vault.crypto import (
    deserialize_envelope,
    serialize_envelope,
)
from wax.state.credential_models import (
    ConnectorDefinitionRecord,
    CredentialEventRecord,
    CredentialGrantRecord,
    PrincipalConnectionRecord,
)

log = get_logger(__name__)


# P0-Taxonomy: The core runtime does NOT hardcode any connector types.
# The intelligence discovers what's needed through generic mechanisms
# (search, web.open, browser). Provider-specific code lives in optional
# adapters, not in the core. The connector_definitions table is empty
# by default; operators may seed it via configuration if they want to
# declare specific connector types for their deployment.
UNIVERSAL_CONNECTORS: list[dict[str, Any]] = []


# Development-mode XOR cipher key. LOUDLY LOGGED as a warning when used.
# Production deployments MUST set WAX_VAULT_KEY.
_DEV_KEY = b"WAX_DEV_VAULT_KEY_DO_NOT_USE_IN_PRODUCTION_32B"


def _get_vault_key() -> bytes:
    """Read the vault encryption key from WAX_VAULT_KEY. Delegates to
    crypto.py's _get_master_key(). Kept for backward compat."""
    from wax.runtime.vault.crypto import _get_master_key

    return _get_master_key()


async def seed_builtin_connectors(session: AsyncSession) -> int:
    """Seed the universal connector definitions. Idempotent."""
    count = 0
    for conn in UNIVERSAL_CONNECTORS:
        existing = (
            await session.execute(
                select(ConnectorDefinitionRecord).where(
                    ConnectorDefinitionRecord.name == conn["name"]
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            record = ConnectorDefinitionRecord(
                id=str(ULID()),
                name=conn["name"],
                description=conn["description"],
                supported_scopes=conn["supported_scopes"],
                auth_methods=conn["auth_methods"],
                version="1.0.0",
            )
            session.add(record)
            count += 1
    if count:
        await session.flush()
        log.info("vault.connectors_seeded", count=count)
    return count


class CredentialVault:
    """The credential vault service.

    All methods take the caller's session; transactions belong to the
    caller. The vault NEVER returns secret values — only opaque IDs
    and handles.
    """

    def __init__(self, settings: Any | None = None) -> None:
        self._settings = settings

    async def connect(
        self,
        session: AsyncSession,
        *,
        principal_id: str,
        connector_name: str,
        secret: str,
        scopes: list[str],
        expires_at: datetime | None = None,
    ) -> str:
        """Register a credential for a connector. Returns connection_id."""
        # Validate the connector exists
        conn = (
            await session.execute(
                select(ConnectorDefinitionRecord).where(
                    ConnectorDefinitionRecord.name == connector_name
                )
            )
        ).scalar_one_or_none()
        if conn is None:
            raise ValueError(f"Unknown connector: {connector_name}")

        # Validate scopes are supported
        unsupported = [s for s in scopes if s not in conn.supported_scopes]
        if unsupported:
            raise ValueError(
                f"Scopes not supported by {connector_name}: {unsupported}. "
                f"Supported: {conn.supported_scopes}"
            )

        # Encrypt the secret at rest using AES-GCM envelope encryption
        from wax.runtime.vault.crypto import encrypt_secret as aes_encrypt

        envelope = aes_encrypt(
            secret,
            record_id="",  # will be set after the record is created
            principal_id=principal_id,
        )
        encrypted_secret = serialize_envelope(envelope)

        # Check for an existing active connection for the same (principal, connector)
        existing = (
            await session.execute(
                select(PrincipalConnectionRecord).where(
                    PrincipalConnectionRecord.principal_id == principal_id,
                    PrincipalConnectionRecord.connector_name == connector_name,
                    PrincipalConnectionRecord.status == "active",
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Rotate: revoke the old, create new
            existing.status = "revoked"
            existing.revoked_at = datetime.now(UTC)
            existing.revoked_reason = "rotated to new connection"
            await self._record_event(
                session,
                principal_id=principal_id,
                connection_id=existing.id,
                kind="rotated",
                connector_name=connector_name,
                scopes=scopes,
            )

        record = PrincipalConnectionRecord(
            id=str(ULID()),
            principal_id=principal_id,
            connector_name=connector_name,
            status="active",
            granted_scopes=scopes,
            secret_blob=encrypted_secret,
            consented_at=datetime.now(UTC),
            expires_at=expires_at,
        )
        session.add(record)
        await session.flush()

        await self._record_event(
            session,
            principal_id=principal_id,
            connection_id=record.id,
            kind="connected",
            connector_name=connector_name,
            scopes=scopes,
        )

        log.info(
            "vault.connected",
            principal_id=principal_id,
            connector_name=connector_name,
            connection_id=record.id,
            scopes=scopes,
        )
        return record.id

    async def list_connections(
        self, session: AsyncSession, *, principal_id: str
    ) -> list[dict[str, Any]]:
        """List the principal's connections. Metadata only — NO secrets."""
        records = (
            (
                await session.execute(
                    select(PrincipalConnectionRecord)
                    .where(PrincipalConnectionRecord.principal_id == principal_id)
                    .order_by(PrincipalConnectionRecord.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "connection_id": r.id,
                "connector": r.connector_name,
                "status": r.status,
                "scopes": r.granted_scopes,
                "consented_at": r.consented_at.isoformat() if r.consented_at else None,
                "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
                "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
                "revoked_reason": r.revoked_reason,
            }
            for r in records
        ]

    async def request_grant(
        self,
        session: AsyncSession,
        *,
        principal_id: str,
        connection_id: str,
        scopes: list[str],
        purpose: str | None = None,
        objective_id: str | None = None,
        execution_id: str | None = None,
        ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Request a scoped grant for an objective/execution.

        Returns {grant_id, handle, expires_at}. The handle is opaque;
        the intelligence uses it to reference the grant. The vault
        injects the actual secret into the environment boundary at
        provisioning time — NEVER into the model's prompt.
        """
        # Validate the connection
        conn = await session.get(PrincipalConnectionRecord, connection_id)
        if conn is None:
            raise ValueError(f"No such connection: {connection_id}")
        if conn.principal_id != principal_id:
            raise ValueError("connection belongs to a different principal")
        if conn.status != "active":
            raise ValueError(f"connection is {conn.status}; cannot request grant")

        # Check TTL expiry
        if conn.expires_at is not None:
            expires = conn.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if datetime.now(UTC) > expires:
                conn.status = "expired"
                await self._record_event(
                    session,
                    principal_id=principal_id,
                    connection_id=connection_id,
                    kind="expired",
                    connector_name=conn.connector_name,
                )
                raise ValueError("connection has expired")

        # Validate scopes
        missing = [s for s in scopes if s not in conn.granted_scopes]
        if missing:
            await self._record_event(
                session,
                principal_id=principal_id,
                connection_id=connection_id,
                kind="denied",
                connector_name=conn.connector_name,
                scopes=scopes,
                detail=f"insufficient scopes: missing {missing}",
            )
            raise ValueError(
                f"Connection does not grant scopes {missing}. Granted: {conn.granted_scopes}"
            )

        # Create the grant
        handle = pysecrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        grant = CredentialGrantRecord(
            id=str(ULID()),
            principal_id=principal_id,
            connection_id=connection_id,
            objective_id=objective_id,
            execution_id=execution_id,
            scopes=scopes,
            purpose=(purpose or "")[:500] if purpose else None,
            handle=handle,
            status="active",
            expires_at=expires_at,
        )
        session.add(grant)
        await session.flush()

        # Update last_used_at on the connection
        conn.last_used_at = datetime.now(UTC)

        await self._record_event(
            session,
            principal_id=principal_id,
            connection_id=connection_id,
            grant_id=grant.id,
            kind="injected",
            connector_name=conn.connector_name,
            scopes=scopes,
            detail=purpose,
        )

        log.info(
            "vault.grant_issued",
            principal_id=principal_id,
            connection_id=connection_id,
            grant_id=grant.id,
            connector_name=conn.connector_name,
            scopes=scopes,
        )
        return {
            "grant_id": grant.id,
            "handle": handle,
            "expires_at": expires_at.isoformat(),
            "scopes": scopes,
            "connector": conn.connector_name,
        }

    async def revoke(
        self,
        session: AsyncSession,
        *,
        principal_id: str,
        connection_id: str | None = None,
        grant_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Revoke a connection OR a grant. Immediate."""
        revoked_items: list[str] = []

        if connection_id:
            conn = await session.get(PrincipalConnectionRecord, connection_id)
            if conn is None:
                raise ValueError(f"No such connection: {connection_id}")
            if conn.principal_id != principal_id:
                raise ValueError("connection belongs to a different principal")
            conn.status = "revoked"
            conn.revoked_at = datetime.now(UTC)
            conn.revoked_reason = (reason or "user_revoked")[:500]
            await self._record_event(
                session,
                principal_id=principal_id,
                connection_id=connection_id,
                kind="revoked",
                connector_name=conn.connector_name,
                detail=reason,
            )
            revoked_items.append(f"connection:{connection_id}")

        if grant_id:
            grant = await session.get(CredentialGrantRecord, grant_id)
            if grant is None:
                raise ValueError(f"No such grant: {grant_id}")
            if grant.principal_id != principal_id:
                raise ValueError("grant belongs to a different principal")
            grant.status = "revoked"
            grant.revoked_at = datetime.now(UTC)
            await self._record_event(
                session,
                principal_id=principal_id,
                connection_id=grant.connection_id,
                grant_id=grant_id,
                kind="revoked",
                detail=reason,
            )
            revoked_items.append(f"grant:{grant_id}")

        return {"revoked": revoked_items}

    async def resolve_handle_for_injection(
        self,
        session: AsyncSession,
        *,
        handle: str,
        principal_id: str,
    ) -> str | None:
        """Resolve a handle to the actual secret for IN-BOUNDARY injection.

        This is the SOLE method that returns a secret value. It is
        called ONLY by the environment planner when injecting into a
        subprocess env. The runtime NEVER exposes this to the model.

        Returns None if the handle is invalid, expired, revoked, or
        belongs to a different principal.
        """
        grant = (
            await session.execute(
                select(CredentialGrantRecord).where(CredentialGrantRecord.handle == handle)
            )
        ).scalar_one_or_none()
        if grant is None:
            return None
        if grant.principal_id != principal_id:
            return None
        if grant.status != "active":
            return None
        # Check TTL
        expires = grant.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if datetime.now(UTC) > expires:
            grant.status = "expired"
            await self._record_event(
                session,
                principal_id=principal_id,
                connection_id=grant.connection_id,
                grant_id=grant.id,
                kind="expired",
            )
            return None

        # Decrypt the secret using AES-GCM envelope decryption
        conn = await session.get(PrincipalConnectionRecord, grant.connection_id)
        if conn is None or conn.status != "active":
            return None

        from wax.runtime.vault.crypto import decrypt_secret as aes_decrypt

        try:
            envelope = deserialize_envelope(conn.secret_blob)
            return aes_decrypt(envelope, record_id=conn.id, principal_id=principal_id)
        except Exception as e:
            log.error("vault.decrypt_failed", error=str(e)[:300])
            return None

    async def _record_event(
        self,
        session: AsyncSession,
        *,
        principal_id: str,
        connection_id: str | None = None,
        grant_id: str | None = None,
        kind: str,
        connector_name: str | None = None,
        scopes: list[str] | None = None,
        detail: str | None = None,
    ) -> None:
        """Record an audit event. Never logs the secret."""
        event = CredentialEventRecord(
            id=str(ULID()),
            principal_id=principal_id,
            connection_id=connection_id,
            grant_id=grant_id,
            kind=kind,
            connector_name=connector_name,
            scopes=scopes,
            detail=(detail or "")[:500] if detail else None,
        )
        session.add(event)
        await session.flush()
