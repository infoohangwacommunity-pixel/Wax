"""Authority broker service (ADR-0048, P0-Authority).

The authority broker is the SOLE path through which the intelligence
requests external authority. The model NEVER supplies a raw secret.

Flow:
1. intelligence calls authority.request (purpose + requested_actions)
2. runtime creates a human handoff (if human_required=true)
3. user completes the handoff via the secure control plane
4. runtime encrypts the authority material (AES-GCM)
5. runtime creates an opaque authority grant
6. waiting work resumes with a typed observation
7. intelligence uses authority.use to perform operations
8. raw secret NEVER enters model context

Design principle: The runtime provides affordances, not workflows.
The intelligence decides WHEN authority is needed.
"""

from __future__ import annotations

import hashlib
import secrets as pysecrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.runtime.authority.contracts import (
    AuthorityRequest,
    AuthorityRequestResult,
    AuthorityStatus,
    HandoffStatus,
    MaterialType,
    VerificationStatus,
)
from wax.runtime.logging import get_logger
from wax.runtime.vault.crypto import (
    encrypt_secret as aes_encrypt,
)
from wax.state.authority_broker_models import (
    AuthorityGrantRecord,
    AuthorityMaterialRecord,
    HumanHandoffRecord,
)

log = get_logger(__name__)


class AuthorityBroker:
    """The authority broker service.

    All methods take the caller's session; transactions belong to the
    caller. The broker NEVER returns secret values — only opaque IDs
    and handles.
    """

    async def request_authority(
        self,
        session: AsyncSession,
        *,
        principal_id: str,
        request: AuthorityRequest,
        objective_id: str | None = None,
        execution_id: str | None = None,
    ) -> AuthorityRequestResult:
        """Create a human handoff for authority acquisition.

        The intelligence calls this when it discovers that authority
        is needed for the current objective. The runtime creates a
        handoff; the user completes it through the control plane.
        """
        # Idempotency: check for an existing pending handoff with the same
        # (principal, purpose, execution) — don't create duplicates
        idempotency_key = hashlib.sha256(
            f"{principal_id}:{request.purpose}:{execution_id or ''}".encode()
        ).hexdigest()[:64]

        existing = (
            await session.execute(
                select(HumanHandoffRecord)
                .where(HumanHandoffRecord.principal_id == principal_id)
                .where(HumanHandoffRecord.idempotency_key == idempotency_key)
                .where(
                    HumanHandoffRecord.status.in_(
                        [HandoffStatus.PENDING.value, HandoffStatus.AWAITING_HUMAN.value]
                    )
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            log.info(
                "authority.handoff_idempotent",
                handoff_id=existing.id,
                principal_id=principal_id,
            )
            return AuthorityRequestResult(
                status="awaiting_human",
                handoff_ref=existing.id,
                expires_at=existing.challenge_expires_at,
            )

        # Create a challenge (single-use, short-lived, bound to principal)
        challenge = pysecrets.token_urlsafe(32)
        challenge_hash = hashlib.sha256(challenge.encode()).hexdigest()
        challenge_expires = datetime.now(UTC) + timedelta(minutes=15)

        handoff = HumanHandoffRecord(
            id=str(ULID()),
            principal_id=principal_id,
            objective_id=objective_id,
            execution_id=execution_id,
            origin_reference=request.origin_reference,
            purpose=request.purpose,
            requested_actions_json={"actions": request.requested_actions},
            instructions_text=(
                f"A secure authority step is waiting.\n"
                f"Purpose: {request.purpose}\n"
                f"Open the WAX control panel to complete this step.\n"
                f"The value you enter will be encrypted immediately and never displayed again."
            ),
            status=HandoffStatus.PENDING.value,
            challenge_hash=challenge_hash,
            challenge_expires_at=challenge_expires,
            created_at_col=datetime.now(UTC),
            idempotency_key=idempotency_key,
        )
        session.add(handoff)
        await session.flush()

        # Emit a signal so waiting work can wake when the handoff completes
        from wax.runtime.work.signals import SignalRepository

        await SignalRepository(session).emit(
            f"authority.handoff_created:{handoff.id}",
            payload={
                "handoff_id": handoff.id,
                "purpose": request.purpose[:200],
                "principal_id": principal_id,
            },
            emitted_by="authority_broker",
        )

        log.info(
            "authority.handoff_created",
            handoff_id=handoff.id,
            principal_id=principal_id,
            purpose=request.purpose[:200],
        )

        return AuthorityRequestResult(
            status="awaiting_human",
            handoff_ref=handoff.id,
            expires_at=challenge_expires,
        )

    async def submit_handoff(
        self,
        session: AsyncSession,
        *,
        handoff_id: str,
        principal_id: str,
        secret_value: str,
        material_type: str = MaterialType.OPAQUE_SECRET.value,
    ) -> dict[str, Any]:
        """Submit the secret through the secure control plane.

        Called ONLY by the control plane (never by the model). The
        secret is encrypted immediately (AES-GCM) and stored. An
        opaque authority grant is created. The raw secret is NEVER
        returned or logged.
        """
        handoff = await session.get(HumanHandoffRecord, handoff_id)
        if handoff is None:
            raise ValueError(f"No such handoff: {handoff_id}")
        if handoff.principal_id != principal_id:
            raise ValueError("handoff belongs to a different principal")
        if handoff.status not in (HandoffStatus.PENDING.value, HandoffStatus.AWAITING_HUMAN.value):
            raise ValueError(f"handoff is {handoff.status}; cannot submit")

        # Check challenge expiry
        if handoff.challenge_expires_at is not None:
            expires = handoff.challenge_expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if datetime.now(UTC) > expires:
                handoff.status = HandoffStatus.EXPIRED.value
                raise ValueError("handoff has expired")

        # Encrypt the secret immediately (AES-GCM)
        material_id = str(ULID())
        envelope = aes_encrypt(
            secret_value,
            record_id=material_id,
            principal_id=principal_id,
        )

        material = AuthorityMaterialRecord(
            id=material_id,
            principal_id=principal_id,
            created_by_handoff_id=handoff.id,
            objective_id=handoff.objective_id,
            origin_reference=handoff.origin_reference,
            material_type=material_type,
            ciphertext=envelope.ciphertext,
            ciphertext_nonce=envelope.ciphertext_nonce,
            wrapped_data_key=envelope.wrapped_data_key,
            wrapped_key_nonce=envelope.wrapped_key_nonce,
            key_version=envelope.key_version,
            metadata_json={"purpose": handoff.purpose[:500]},
            verification_status=VerificationStatus.UNVERIFIED.value,
            status=AuthorityStatus.ACTIVE.value,
        )
        session.add(material)

        # Create an opaque authority grant
        grant_handle = pysecrets.token_urlsafe(32)
        grant = AuthorityGrantRecord(
            id=str(ULID()),
            principal_id=principal_id,
            objective_id=handoff.objective_id,
            execution_id=handoff.execution_id,
            authority_material_id=material_id,
            handle=grant_handle,
            allowed_actions_json=handoff.requested_actions_json,
            effect_class="read_only",
            status="active",
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        session.add(grant)

        # Mark handoff as completed
        handoff.status = HandoffStatus.COMPLETED.value
        handoff.completed_at = datetime.now(UTC)
        handoff.completion_evidence_json = {
            "material_id": material_id,
            "grant_id": grant.id,
            "material_type": material_type,
        }

        # Emit a signal so waiting work can wake
        from wax.runtime.work.signals import SignalRepository

        await SignalRepository(session).emit(
            f"authority.handoff_completed:{handoff.id}",
            payload={
                "handoff_id": handoff.id,
                "authority_ref": grant.handle,
                "verification_status": VerificationStatus.UNVERIFIED.value,
            },
            emitted_by="authority_broker",
        )

        log.info(
            "authority.handoff_completed",
            handoff_id=handoff.id,
            material_id=material_id,
            grant_id=grant.id,
            principal_id=principal_id,
        )

        return {
            "status": "stored",
            "handoff_id": handoff_id,
            "authority_ref": grant_handle,
            "verification_status": VerificationStatus.UNVERIFIED.value,
            "expires_at": grant.expires_at.isoformat(),
        }

    async def get_status(
        self,
        session: AsyncSession,
        *,
        handoff_ref: str,
        principal_id: str,
    ) -> dict[str, Any]:
        """Check the status of an authority request."""
        handoff = await session.get(HumanHandoffRecord, handoff_ref)
        if handoff is None:
            return {"status": "not_found"}
        if handoff.principal_id != principal_id:
            return {"status": "denied"}

        result = {
            "status": handoff.status,
            "expires_at": handoff.challenge_expires_at.isoformat()
            if handoff.challenge_expires_at
            else None,
        }

        if handoff.status == HandoffStatus.COMPLETED.value:
            grant = (
                await session.execute(
                    select(AuthorityGrantRecord)
                    .where(AuthorityGrantRecord.principal_id == principal_id)
                    .order_by(AuthorityGrantRecord.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if grant and grant.status == "active":
                result["authority_ref"] = grant.handle
                result["grant_status"] = grant.status
                result["grant_expires_at"] = grant.expires_at.isoformat()

        return result

    async def revoke_authority(
        self,
        session: AsyncSession,
        *,
        authority_ref: str,
        principal_id: str,
    ) -> bool:
        """Revoke an authority grant immediately."""
        grant = (
            await session.execute(
                select(AuthorityGrantRecord).where(AuthorityGrantRecord.handle == authority_ref)
            )
        ).scalar_one_or_none()

        if grant is None:
            return False
        if grant.principal_id != principal_id:
            return False

        grant.status = "revoked"
        grant.revoked_at = datetime.now(UTC)

        # Also revoke the underlying material
        material = await session.get(AuthorityMaterialRecord, grant.authority_material_id)
        if material and material.status == AuthorityStatus.ACTIVE.value:
            material.status = AuthorityStatus.REVOKED.value
            material.revoked_at = datetime.now(UTC)

        log.info(
            "authority.revoked",
            grant_id=grant.id,
            principal_id=principal_id,
        )
        return True
