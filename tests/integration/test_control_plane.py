"""Control plane HTTP layer — integration tests (ADR-0048).

The ADR specified a server-rendered same-origin dashboard with
`GET /control/handoffs/{id}` and `POST /control/handoffs/{id}/submit`
but it was never implemented — submit_handoff had no human-reachable
entrypoint. These tests pin the new surface: auth, CSRF, metadata-only
rendering, immediate encryption, and the never-echo guarantee.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from wax.authority.seed import seed_builtin_roles
from wax.identity.repository import PrincipalRepository
from wax.runtime.app import create_app
from wax.runtime.authority.broker import AuthorityBroker
from wax.runtime.authority.contracts import AuthorityRequest
from wax.state.authority_broker_models import (
    AuthorityGrantRecord,
    AuthorityMaterialRecord,
    HumanHandoffRecord,
)
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base

pytestmark = pytest.mark.integration

SECRET_TYPED_BY_HUMAN = "operator-typed-secret-DO-NOT-ECHO-9f2c1"


async def _create_principal(*, display_name: str = "Operator", phone: str = "1234567890") -> str:
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


async def _create_handoff(principal_id: str, *, kind: str = "secret") -> str:
    purpose = (
        "Deploy service that needs an API credential"
        if kind == "secret"
        else "Vendor portal requires a human login before reporting"
    )
    instructions = (
        "Open the vendor console, create a read-only API key, paste it below."
        if kind == "secret"
        else "Log into the vendor console in your browser, then export the session."
    )
    async with db_session() as s:
        broker = AuthorityBroker()
        result = await broker.request_authority(
            s,
            principal_id=principal_id,
            request=AuthorityRequest(
                purpose=purpose,
                origin_reference="https://example.com/console",
                requested_actions=[{"description": "store an API key", "effect_class": "write"}],
                handoff_kind=kind,
                instructions_text=instructions,
            ),
        )
        await s.commit()
        return result.handoff_ref


def _csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "form must embed a csrf token"
    return match.group(1)


@pytest.fixture
async def db():
    test_app_settings = None
    from wax.core.config import settings_for_testing

    test_app_settings = settings_for_testing()
    test_app_settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(test_app_settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as s:
        await seed_builtin_roles(s)
        await s.commit()
    yield
    await dispose_engine()


@pytest.fixture
async def app(db):
    from wax.core.config import settings_for_testing

    settings = settings_for_testing()
    settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    application = create_app(settings=settings)
    yield application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as ac:
        yield ac


class TestDashboardAccess:
    async def test_dashboard_renders_in_dev_without_token(self, client, app):
        resp = await client.get("/control")
        assert resp.status_code == 200
        assert "Control Plane" in resp.text
        # Same-origin hardening headers are present.
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.headers["X-Frame-Options"] == "DENY"
        # No external assets: no CDN/host links in the HTML.
        assert 'src="http' not in resp.text and 'href="http' not in resp.text

    async def test_production_without_token_refuses_to_serve(self, db):
        from wax.core.config import Environment, settings_for_testing

        settings = settings_for_testing(env=Environment.PRODUCTION)
        settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
        application = create_app(settings=settings)
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/control")
        assert resp.status_code == 503
        assert "WAX_CONTROL_PLANE_TOKEN" in resp.text

    async def test_login_flow_with_token(self, client, app):
        app.state.settings.control_plane_token = "op-token-777"
        try:
            # Not authenticated yet → redirected to the login form.
            resp = await client.get("/control")
            assert resp.status_code == 200
            assert "Operator sign-in" in resp.text

            # Wrong token → 401, no session established.
            bad = await client.post("/control/login", data={"token": "wrong"})
            assert bad.status_code == 401
            assert "wax_control_session" not in client.cookies

            good = await client.post("/control/login", data={"token": "op-token-777"})
            assert good.status_code == 200  # followed redirect → dashboard
            # The signed, HttpOnly session cookie was issued to the jar.
            assert "wax_control_session" in client.cookies

            dashboard = await client.get("/control")
            assert dashboard.status_code == 200
            assert "Operator sign-in" not in dashboard.text
        finally:
            app.state.settings.control_plane_token = ""

    async def test_bearer_header_authentication(self, client, app):
        app.state.settings.control_plane_token = "op-token-888"
        try:
            resp = await client.get("/control", headers={"Authorization": "Bearer op-token-888"})
            assert resp.status_code == 200
        finally:
            app.state.settings.control_plane_token = ""


class TestHandoffPages:
    async def test_handoff_detail_renders_metadata_only(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)

        resp = await client.get(f"/control/handoffs/{handoff_id}")
        assert resp.status_code == 200
        assert "Deploy service that needs an API credential" in resp.text
        assert "read-only API key" in resp.text  # instructions shown
        # The form exists and binds csrf.
        csrf = _csrf_from(resp.text)
        assert len(csrf) > 20

    async def test_viewing_marks_handoff_opened(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        await client.get(f"/control/handoffs/{handoff_id}")

        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
        assert handoff.status == "opened"

    async def test_unknown_handoff_404(self, client, app):
        resp = await client.get("/control/handoffs/01NOTREAL00000000000000")
        assert resp.status_code == 404


class TestHandoffSubmit:
    async def test_submit_encrypts_and_completes_handoff(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)

        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)

        resp = await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": SECRET_TYPED_BY_HUMAN},
        )
        assert resp.status_code == 200

        # The submitted value is NEVER echoed back.
        assert SECRET_TYPED_BY_HUMAN not in resp.text

        # The handoff completed; material + grant exist.
        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            assert handoff.status == "completed"
            material = (
                await s.execute(
                    select(AuthorityMaterialRecord).where(
                        AuthorityMaterialRecord.created_by_handoff_id == handoff_id
                    )
                )
            ).scalar_one()
            grant = (
                await s.execute(
                    select(AuthorityGrantRecord).where(
                        AuthorityGrantRecord.authority_material_id == material.id
                    )
                )
            ).scalar_one()
            assert grant.status == "active"

        assert material.material_type == "opaque_secret"
        # The ciphertext must not contain the plaintext.
        assert SECRET_TYPED_BY_HUMAN not in material.ciphertext

    async def test_submit_rejects_stale_csrf(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)

        resp = await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": "1.deadbeef.deadbeef", "secret_value": SECRET_TYPED_BY_HUMAN},
        )
        assert resp.status_code == 403
        assert SECRET_TYPED_BY_HUMAN not in resp.text

        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
        assert handoff.status != "completed"

    async def test_submit_rejects_empty_value(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)

        resp = await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": "   "},
        )
        assert resp.status_code == 400
        assert "must not be empty" in resp.text

    async def test_submit_on_completed_handoff_rejected(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)
        first = await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": "first-value"},
        )
        assert first.status_code == 200

        # A second submission attempt is refused by the broker.
        second = await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": "second-value"},
        )
        assert second.status_code == 400

    async def test_expired_handoff_refused(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        # Force the challenge to be expired.
        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            handoff.challenge_expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()

        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)
        resp = await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": SECRET_TYPED_BY_HUMAN},
        )
        assert resp.status_code == 400
        assert "expired" in resp.text


class TestDashboardListing:
    async def test_dashboard_lists_handoffs(self, client, app):
        principal_id = await _create_principal()
        h1 = await _create_handoff(principal_id)
        h2 = await _create_handoff(principal_id, kind="browser")

        resp = await client.get("/control")
        assert resp.status_code == 200
        assert h1[:12] in resp.text
        assert h2[:12] in resp.text
        assert "browser" in resp.text  # kind badge
