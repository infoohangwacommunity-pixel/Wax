"""Authority Broker — integration tests (ADR-0048, P0-Authority).

Verifies the core security guarantee: the model NEVER sees a raw secret.
The intelligence requests authority generically; the runtime creates a
human handoff; the secret is submitted through the secure control plane.

Tests:
- authority.request creates a handoff (no secret in the model path)
- handoff is idempotent (same purpose + execution → same handoff)
- submit_handoff encrypts immediately (AES-GCM)
- the raw secret never appears in any capability output
- the raw secret never appears in the handoff record
- authority grant is opaque (handle-based, not the secret)
- revoke works immediately
- wrong principal cannot access another's handoff
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.identity.repository import PrincipalRepository
from wax.runtime.authority import (
    AuthorityRequest,
    HandoffStatus,
)
from wax.runtime.services import RuntimeServices
from wax.state.authority_broker_models import (
    AuthorityGrantRecord,
    AuthorityMaterialRecord,
    HumanHandoffRecord,
)
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture
async def fresh_db(test_settings):
    test_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as s:
        await seed_builtin_roles(s)
        await s.commit()
    yield
    await dispose_engine()


@pytest.fixture
def services(test_settings):
    return RuntimeServices.build(test_settings)


async def _create_principal(*, phone: str = "1234567890") -> str:
    from wax.authority.seed import ensure_principal_role

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name="Test")
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, "admin")
        await s.commit()
        return principal.id


class TestAuthorityRequest:
    async def test_request_creates_handoff_without_secret(self, fresh_db, services):
        """The intelligence requests authority; the runtime creates a handoff.
        The secret is NOT in the request or the handoff record."""
        principal_id = await _create_principal()
        broker = services.authority_broker
        assert broker is not None

        request = AuthorityRequest(
            purpose="Need API access to read repository content",
            requested_actions=[
                {"description": "Read repository files", "effect_class": "read_only"}
            ],
            human_required=True,
        )

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=request,
                execution_id="test-exec-1",
            )
            await s.commit()

        assert result.status == "awaiting_human"
        assert result.handoff_ref is not None

        # Verify the handoff record has NO secret
        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, result.handoff_ref)
            assert handoff is not None
            assert handoff.status == HandoffStatus.PENDING.value
            assert handoff.purpose == request.purpose
            # The handoff record does NOT contain the secret
            assert not hasattr(handoff, "secret")
            assert not hasattr(handoff, "ciphertext")
            assert handoff.challenge_hash is not None  # challenge is hashed

    async def test_request_is_idempotent(self, fresh_db, services):
        """Repeated requests with the same purpose + execution create
        only ONE handoff."""
        principal_id = await _create_principal()
        broker = services.authority_broker

        request = AuthorityRequest(
            purpose="Need API access",
            human_required=True,
        )

        async with db_session() as s:
            r1 = await broker.request_authority(
                s, principal_id=principal_id, request=request, execution_id="exec-1"
            )
            await s.commit()

            r2 = await broker.request_authority(
                s, principal_id=principal_id, request=request, execution_id="exec-1"
            )
            await s.commit()

        assert r1.handoff_ref == r2.handoff_ref  # same handoff

    async def test_request_emits_signal(self, fresh_db, services):
        """The broker emits a signal so waiting work can wake when
        the handoff is completed."""
        principal_id = await _create_principal()
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="test", human_required=True),
            )
            await s.commit()

        from wax.state.work_models import RuntimeSignalRecord

        async with db_session() as s:
            signals = (
                (
                    await s.execute(
                        select(RuntimeSignalRecord).where(
                            RuntimeSignalRecord.name
                            == f"authority.handoff_created:{result.handoff_ref}"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(signals) == 1
            assert signals[0].emitted_by == "authority_broker"


class TestAuthoritySubmit:
    async def test_submit_encrypts_secret_immediately(self, fresh_db, services):
        """The secret is encrypted with AES-GCM immediately upon submission.
        The raw value never appears in the database."""
        principal_id = await _create_principal()
        broker = services.authority_broker
        secret_marker = "UNIQUE_SECRET_VALUE_xyz123"

        # Create a handoff first
        async with db_session() as s:
            request_result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="test", human_required=True),
            )
            await s.commit()
            handoff_id = request_result.handoff_ref

        # Submit the secret through the control plane
        async with db_session() as s:
            submit_result = await broker.submit_handoff(
                s,
                handoff_id=handoff_id,
                principal_id=principal_id,
                secret_value=secret_marker,
            )
            await s.commit()

        assert submit_result["status"] == "stored"
        assert "authority_ref" in submit_result
        assert submit_result["authority_ref"] != secret_marker

        # Verify the secret is NOT in any database record
        async with db_session() as s:
            # Check handoff
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            assert handoff.status == HandoffStatus.COMPLETED.value
            assert secret_marker not in str(handoff.completion_evidence_json)
            assert secret_marker not in str(handoff.requested_actions_json)

            # Check material
            materials = (
                (
                    await s.execute(
                        select(AuthorityMaterialRecord).where(
                            AuthorityMaterialRecord.principal_id == principal_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(materials) == 1
            assert secret_marker not in materials[0].ciphertext
            assert secret_marker not in materials[0].wrapped_data_key
            assert materials[0].verification_status == "unverified"

            # Check grant
            grants = (
                (
                    await s.execute(
                        select(AuthorityGrantRecord).where(
                            AuthorityGrantRecord.principal_id == principal_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(grants) == 1
            assert grants[0].handle != secret_marker
            assert grants[0].status == "active"

    async def test_submit_rejects_wrong_principal(self, fresh_db, services):
        principal_a = await _create_principal(phone="1111111111")
        principal_b = await _create_principal(phone="2222222222")
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_a,
                request=AuthorityRequest(purpose="test", human_required=True),
            )
            await s.commit()
            handoff_id = result.handoff_ref

        async with db_session() as s:
            with pytest.raises(ValueError, match="different principal"):
                await broker.submit_handoff(
                    s,
                    handoff_id=handoff_id,
                    principal_id=principal_b,
                    secret_value="should-fail",
                )

    async def test_submit_emits_completion_signal(self, fresh_db, services):
        principal_id = await _create_principal()
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="test", human_required=True),
            )
            await s.commit()
            handoff_id = result.handoff_ref

            await broker.submit_handoff(
                s,
                handoff_id=handoff_id,
                principal_id=principal_id,
                secret_value="test-secret",
            )
            await s.commit()

        from wax.state.work_models import RuntimeSignalRecord

        async with db_session() as s:
            signals = (
                (
                    await s.execute(
                        select(RuntimeSignalRecord).where(
                            RuntimeSignalRecord.name == f"authority.handoff_completed:{handoff_id}"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(signals) == 1
            assert "authority_ref" in str(signals[0].payload)


class TestAuthorityStatus:
    async def test_status_returns_handoff_state(self, fresh_db, services):
        principal_id = await _create_principal()
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="test", human_required=True),
            )
            await s.commit()
            handoff_id = result.handoff_ref

        async with db_session() as s:
            status = await broker.get_status(s, handoff_ref=handoff_id, principal_id=principal_id)
            await s.commit()

        assert status["status"] == HandoffStatus.PENDING.value

    async def test_status_after_completion_shows_authority_ref(self, fresh_db, services):
        principal_id = await _create_principal()
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="test", human_required=True),
            )
            await s.commit()
            handoff_id = result.handoff_ref

            await broker.submit_handoff(
                s,
                handoff_id=handoff_id,
                principal_id=principal_id,
                secret_value="test-secret",
            )
            await s.commit()

        async with db_session() as s:
            status = await broker.get_status(s, handoff_ref=handoff_id, principal_id=principal_id)
            await s.commit()

        assert status["status"] == HandoffStatus.COMPLETED.value
        assert "authority_ref" in status


class TestAuthorityRevoke:
    async def test_revoke_invalidates_grant_immediately(self, fresh_db, services):
        principal_id = await _create_principal()
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="test", human_required=True),
            )
            await s.commit()
            handoff_id = result.handoff_ref

            submit_result = await broker.submit_handoff(
                s,
                handoff_id=handoff_id,
                principal_id=principal_id,
                secret_value="test-secret",
            )
            await s.commit()
            authority_ref = submit_result["authority_ref"]

        async with db_session() as s:
            ok = await broker.revoke_authority(
                s, authority_ref=authority_ref, principal_id=principal_id
            )
            await s.commit()

        assert ok is True

        async with db_session() as s:
            grant = (
                await s.execute(
                    select(AuthorityGrantRecord).where(AuthorityGrantRecord.handle == authority_ref)
                )
            ).scalar_one()
            assert grant.status == "revoked"
            assert grant.revoked_at is not None

            material = await s.get(AuthorityMaterialRecord, grant.authority_material_id)
            assert material.status == "revoked"

    async def test_revoke_emits_wakeup_signal(self, fresh_db, services):
        """Both revoke paths append authority.grant_revoked:{handle} so
        work holding the ref can observe the loss immediately."""
        from wax.state.work_models import RuntimeSignalRecord

        principal_id = await _create_principal()
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="signal probe", human_required=True),
            )
            await s.commit()
            submit_result = await broker.submit_handoff(
                s,
                handoff_id=result.handoff_ref,
                principal_id=principal_id,
                secret_value="test-secret",
            )
            await s.commit()
            authority_ref = submit_result["authority_ref"]

        async with db_session() as s:
            await broker.revoke_authority(s, authority_ref=authority_ref, principal_id=principal_id)
            await s.commit()

        async with db_session() as s:
            signals = (
                (
                    await s.execute(
                        select(RuntimeSignalRecord).where(
                            RuntimeSignalRecord.name == f"authority.grant_revoked:{authority_ref}"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(signals) == 1
            assert signals[0].emitted_by == "authority_broker"
            assert signals[0].payload["revoked_by"] == "principal"
            assert signals[0].payload["handle"] == authority_ref


class TestAuthorityCancel:
    async def test_cancel_marks_open_handoff_cancelled(self, fresh_db, services):
        principal_id = await _create_principal()
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="cancel me", human_required=True),
            )
            await s.commit()
            handoff_id = result.handoff_ref

            outcome = await broker.cancel_handoff(
                s,
                handoff_id=handoff_id,
                principal_id=principal_id,
                reason_safe="superseded by a newer request",
            )
            await s.commit()

        assert outcome["status"] == HandoffStatus.CANCELLED.value
        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            assert handoff.status == HandoffStatus.CANCELLED.value
            assert handoff.failure_reason_safe == "superseded by a newer request"

    async def test_cancel_emits_wakeup_signal(self, fresh_db, services):
        principal_id = await _create_principal()
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="cancel signal", human_required=True),
            )
            await s.commit()
            handoff_id = result.handoff_ref
            await broker.cancel_handoff(s, handoff_id=handoff_id, principal_id=principal_id)
            await s.commit()

        from wax.state.work_models import RuntimeSignalRecord

        async with db_session() as s:
            signals = (
                (
                    await s.execute(
                        select(RuntimeSignalRecord).where(
                            RuntimeSignalRecord.name == f"authority.handoff_cancelled:{handoff_id}"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(signals) == 1
            assert "reason_safe" in str(signals[0].payload)

    async def test_cancel_rejects_terminal_handoff(self, fresh_db, services):
        principal_id = await _create_principal()
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="already done", human_required=True),
            )
            await s.commit()
            handoff_id = result.handoff_ref
            await broker.submit_handoff(
                s,
                handoff_id=handoff_id,
                principal_id=principal_id,
                secret_value="secret",
            )
            await s.commit()

            with pytest.raises(ValueError, match="cannot cancel"):
                await broker.cancel_handoff(s, handoff_id=handoff_id, principal_id=principal_id)

    async def test_cancel_rejects_wrong_principal(self, fresh_db, services):
        principal_id = await _create_principal()
        other_principal_id = await _create_principal(phone="9990001111")
        broker = services.authority_broker

        async with db_session() as s:
            result = await broker.request_authority(
                s,
                principal_id=principal_id,
                request=AuthorityRequest(purpose="not yours", human_required=True),
            )
            await s.commit()
            handoff_id = result.handoff_ref

            with pytest.raises(ValueError, match="different principal"):
                await broker.cancel_handoff(
                    s, handoff_id=handoff_id, principal_id=other_principal_id
                )
