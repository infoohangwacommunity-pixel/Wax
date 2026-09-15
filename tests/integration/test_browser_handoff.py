"""Browser handoff capability — integration tests (P0-Browser).

`MaterialType.BROWSER_SESSION_REFERENCE` existed as an enum value with
no producer and no consumer. These tests pin the new end-to-end path:
browser.handoff creates a `browser`-kind human handoff → the human
submits a browser session reference through the control plane → the
material is stored AS a browser_session_reference → authority.status
reports an opaque handle to the intelligence.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.capabilities.contracts import CapabilityInvocationRequest
from wax.identity.repository import PrincipalRepository
from wax.runtime.authority.broker import AuthorityBroker
from wax.runtime.authority.contracts import AuthorityRequest
from wax.runtime.services import RuntimeServices
from wax.state.authority_broker_models import (
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


async def _create_principal(*, display_name: str = "Test", phone: str = "1234567890") -> str:
    from wax.authority.seed import ensure_principal_role

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name=display_name)
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, "admin")
        await s.commit()
        return principal.id


class TestBrowserHandoffCapability:
    async def test_capability_is_registered(self, services):
        descriptor, _impl = services.capability_registry.get("browser.handoff")
        assert descriptor is not None
        assert "browser" in descriptor.name

    async def test_browser_handoff_creates_browser_kind_handoff(self, fresh_db, services):
        principal_id = await _create_principal()

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="browser.handoff",
                    principal_id=principal_id,
                    inputs={
                        "purpose": "Site requires a human login before reporting",
                        "origin_reference": "https://portal.example.com/login",
                        "requested_actions": [
                            {
                                "description": "log into the portal and export the session",
                                "effect_class": "write",
                            }
                        ],
                        "instructions": (
                            "Open https://portal.example.com/login in YOUR browser, "
                            "sign in with your own account, complete the 2FA prompt, "
                            "then export the session cookie and paste it into the form."
                        ),
                    },
                )
            )
            await s.commit()

        assert result.outcome == "success", result.error
        outputs = result.outputs
        assert outputs["status"] == "awaiting_human"
        assert outputs["handoff_ref"]
        assert outputs["control_plane_path"] == f"/control/handoffs/{outputs['handoff_ref']}"

        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, outputs["handoff_ref"])
        assert handoff is not None
        assert handoff.principal_id == principal_id
        assert handoff.requested_actions_json["handoff_kind"] == "browser"
        assert "YOUR browser" in (handoff.instructions_text or "")

    async def test_browser_handoff_idempotent_at_broker_level(self, fresh_db, services):
        """Idempotency lives in the authority broker: the same
        (principal, purpose, execution) returns the SAME handoff instead
        of creating duplicates."""
        principal_id = await _create_principal()
        request = AuthorityRequest(
            purpose="Same browser step",
            instructions_text="Do the browser step",
            handoff_kind="browser",
        )

        async with db_session() as s:
            broker = AuthorityBroker()
            r1 = await broker.request_authority(
                s, principal_id=principal_id, request=request, execution_id="exec-1"
            )
            await s.commit()
            r2 = await broker.request_authority(
                s, principal_id=principal_id, request=request, execution_id="exec-1"
            )
            await s.commit()

        assert r1.handoff_ref == r2.handoff_ref
        assert r2.status == "awaiting_human"


class TestBrowserHandoffCompletion:
    async def test_submit_stores_browser_session_reference(self, fresh_db, services):
        principal_id = await _create_principal()

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="browser.handoff",
                    principal_id=principal_id,
                    inputs={"purpose": "Login needed", "instructions": "log in please"},
                )
            )
            await s.commit()
            handoff_ref = result.outputs["handoff_ref"]

        session_blob = "SESSION-REF-CONTENT-NOT-A-SECRET-IN-TEST"
        async with db_session() as s:
            broker = AuthorityBroker()
            submit = await broker.submit_handoff(
                s,
                handoff_id=handoff_ref,
                principal_id=principal_id,
                secret_value=session_blob,
                material_type="browser_session_reference",
            )
            await s.commit()

        assert submit["status"] == "stored"
        assert submit["authority_ref"]

        # The material is stored AS a browser_session_reference, encrypted.
        async with db_session() as s:
            material = (
                await s.execute(
                    select(AuthorityMaterialRecord).where(
                        AuthorityMaterialRecord.created_by_handoff_id == handoff_ref
                    )
                )
            ).scalar_one()
        assert material.material_type == "browser_session_reference"
        assert session_blob not in material.ciphertext

    async def test_opaque_secret_material_refused_for_browser_handoff(self, fresh_db, services):
        """A browser handoff yields a browser session reference — the
        control plane cannot silently store it as an 'opaque secret'."""
        principal_id = await _create_principal()

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="browser.handoff",
                    principal_id=principal_id,
                    inputs={"purpose": "Login needed"},
                )
            )
            await s.commit()
            handoff_ref = result.outputs["handoff_ref"]

        async with db_session() as s:
            broker = AuthorityBroker()
            with pytest.raises(ValueError, match="does not match this handoff"):
                await broker.submit_handoff(
                    s,
                    handoff_id=handoff_ref,
                    principal_id=principal_id,
                    secret_value="some-value",
                    material_type="opaque_secret",
                )

    async def test_authority_status_reports_material_type(self, fresh_db, services):
        principal_id = await _create_principal()

        async with db_session() as s:
            invoker = services.invoker(s)
            result = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="browser.handoff",
                    principal_id=principal_id,
                    inputs={"purpose": "Login needed"},
                )
            )
            await s.commit()
            handoff_ref = result.outputs["handoff_ref"]

        async with db_session() as s:
            invoker = services.invoker(s)
            pre = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="authority.status",
                    principal_id=principal_id,
                    inputs={"handoff_ref": handoff_ref},
                )
            )
            await s.commit()
        assert pre.outputs["status"] in ("pending", "opened", "awaiting_human")
        assert "authority_ref" not in pre.outputs

        async with db_session() as s:
            broker = AuthorityBroker()
            await broker.submit_handoff(
                s,
                handoff_id=handoff_ref,
                principal_id=principal_id,
                secret_value="the-session-reference",
                material_type="browser_session_reference",
            )
            await s.commit()

        async with db_session() as s:
            invoker = services.invoker(s)
            post = await invoker.invoke(
                CapabilityInvocationRequest(
                    capability_name="authority.status",
                    principal_id=principal_id,
                    inputs={"handoff_ref": handoff_ref},
                )
            )
            await s.commit()

        assert post.outputs["status"] == "completed"
        assert post.outputs["material_type"] == "browser_session_reference"
        assert post.outputs["authority_ref"]
        # The reference VALUE must never surface through status.
        assert "the-session-reference" not in str(post.outputs)
