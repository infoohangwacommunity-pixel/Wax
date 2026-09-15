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


async def _create_handoff(
    principal_id: str, *, kind: str = "secret", purpose: str | None = None
) -> str:
    if purpose is None:
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


def _csrf_from_form(html: str, action_fragment: str) -> str:
    """Extract the csrf token from THE form whose action contains
    action_fragment — pages can carry multiple forms (submit, cancel,
    GC, logout) with independently scoped tokens."""
    form_re = re.compile(
        r'<form[^>]*action="[^"]*' + re.escape(action_fragment) + r'[^"]*"[^>]*>(.*?)</form>',
        re.DOTALL,
    )
    match = form_re.search(html)
    assert match, f"no form with action containing {action_fragment!r}"
    token = re.search(r'name="csrf_token" value="([^"]+)"', match.group(1))
    assert token, "form must embed a csrf token"
    return token.group(1)


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

    async def test_expired_handoff_shows_expired_panel_not_form(self, client, app):
        """An expired challenge is NOT submittable: the detail page renders
        an explicit expired panel (no submit form, no submit csrf) so an
        operator never types a secret the runtime would refuse. (A cancel
        form may legitimately appear — cancelling an expired-but-unmarked
        handoff is a supported cleanup action.)"""
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        # Force the challenge to be expired.
        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            handoff.challenge_expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()

        page = await client.get(f"/control/handoffs/{handoff_id}")
        assert page.status_code == 200
        assert "has expired" in page.text
        assert 'name="secret_value"' not in page.text
        assert 'name="material_type"' not in page.text
        assert f'action="/control/handoffs/{handoff_id}/submit"' not in page.text

    async def test_expired_handoff_submit_still_refused_by_broker(self, client, app):
        """Defense in depth: even a hand-forged valid CSRF token cannot
        submit an expired handoff — the broker refuses it (400, 'expired')."""
        from wax.runtime.control_plane import ControlPlaneAuth

        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            handoff.challenge_expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()

        auth = ControlPlaneAuth(app.state.settings)
        csrf = auth.make_csrf()
        resp = await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": SECRET_TYPED_BY_HUMAN},
        )
        assert resp.status_code == 400
        assert "expired" in resp.text
        assert SECRET_TYPED_BY_HUMAN not in resp.text

    async def test_expired_handoff_renders_expired_on_dashboard(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            handoff.challenge_expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()

        resp = await client.get("/control")
        assert resp.status_code == 200
        row = resp.text
        assert ">expired</span>" in row or "expired" in row


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


class TestDashboardFiltersAndRefresh:
    async def test_filter_tabs_render_with_counts(self, client, app):
        principal_id = await _create_principal()
        await _create_handoff(principal_id)
        resp = await client.get("/control")
        assert 'href="/control?status=open"' in resp.text
        assert 'href="/control?status=completed"' in resp.text
        assert 'href="/control?status=failed"' in resp.text
        assert 'href="/control?status=all"' in resp.text

    async def test_status_filter_narrows_rows(self, client, app):
        principal_id = await _create_principal()
        # Distinct purposes: the broker dedups identical OPEN requests, so
        # two same-purpose handoffs would collapse into one record.
        open_id = await _create_handoff(principal_id, purpose="Needs credential A")
        done_id = await _create_handoff(principal_id, purpose="Needs credential B")
        page = await client.get(f"/control/handoffs/{done_id}")
        csrf = _csrf_from(page.text)
        resp = await client.post(
            f"/control/handoffs/{done_id}/submit",
            data={"csrf_token": csrf, "secret_value": "value-1"},
        )
        assert resp.status_code == 200

        completed_view = await client.get("/control?status=completed")
        # Full IDs appear in row hrefs — the filtered view must contain the
        # completed handoff and not the open one (ULIDs share time prefixes,
        # so prefix assertions would be flaky).
        assert done_id in completed_view.text
        assert open_id not in completed_view.text

        open_view = await client.get("/control?status=open")
        assert open_id in open_view.text
        assert done_id not in open_view.text

    async def test_unknown_filter_falls_back_to_all(self, client, app):
        await _create_principal()
        resp = await client.get("/control?status=nonsense")
        assert resp.status_code == 200
        assert (
            'class="active" href="/control?status=all"' in resp.text
            or "No handoffs match" not in resp.text
        )

    async def test_auto_refresh_only_while_open_handoffs_exist(self, client, app):
        principal_id = await _create_principal()
        await _create_handoff(principal_id)
        resp = await client.get("/control")
        assert 'http-equiv="refresh"' in resp.text

    async def test_no_auto_refresh_without_open_handoffs(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)
        await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": "value-1"},
        )
        resp = await client.get("/control")
        assert 'http-equiv="refresh"' not in resp.text
        # with zero open handoffs the poller runs in JSON-heartbeat mode
        assert '"/control/api/status"' in resp.text

    async def test_relative_timestamps_rendered(self, client, app):
        principal_id = await _create_principal()
        await _create_handoff(principal_id)
        resp = await client.get("/control")
        assert "just now" in resp.text


class TestMaintenanceGC:
    async def test_gc_route_removes_only_unreferenced_blobs(self, client, app, tmp_path):
        from pathlib import Path as _Path

        from wax.runtime.blob_store import ContentAddressedBlobStore
        from wax.state.workspace_models import WorkspaceSnapshotRecord

        # ASGITransport does not run lifespan, so attach the services
        # container the way the real startup path would.
        if getattr(app.state, "services", None) is None:
            from wax.runtime.services import RuntimeServices

            app.state.services = RuntimeServices.build(app.state.settings)
        store = ContentAddressedBlobStore(_Path(tmp_path) / "blobs")
        app.state.services.blob_store = store

        live_digest = store.put_bytes(b"live workspace file")
        stale_digest = store.put_bytes(b"deleted long ago")
        principal_id = await _create_principal()
        async with db_session() as s:
            from ulid import ULID

            s.add(
                WorkspaceSnapshotRecord(
                    id=str(ULID()),
                    principal_id=principal_id,
                    workspace_resource_id=principal_id,
                    content_hash="0" * 64,
                    files_json=[{"path": "a.txt", "sha256": live_digest, "size": 19}],
                    file_count=1,
                    total_bytes=19,
                    captured_at=datetime.now(UTC),
                )
            )
            await s.commit()

        page = await client.get("/control")
        csrf = _csrf_from(page.text)
        resp = await client.post("/control/maintenance/gc", data={"csrf_token": csrf})
        assert resp.status_code == 200

        assert store.has(live_digest)  # referenced → kept
        assert not store.has(stale_digest)  # unreachable → collected
        assert "removed <strong>1</strong>" in resp.text

    async def test_gc_route_requires_csrf(self, client, app):
        resp = await client.post("/control/maintenance/gc", data={"csrf_token": "bogus"})
        assert resp.status_code == 200  # redirect followed back to dashboard
        assert "rejected" in resp.text  # error flash rendered


class TestLoginRateLimit:
    async def test_brute_force_is_rate_limited(self, client, app):
        from wax.runtime.control_plane import _login_limiter

        _login_limiter.reset()
        app.state.settings.control_plane_token = "op-token-999"
        try:
            for _ in range(8):
                bad = await client.post("/control/login", data={"token": "wrong"})
                assert bad.status_code == 401
            limited = await client.post("/control/login", data={"token": "op-token-999"})
            assert limited.status_code == 429
            assert "Too many failed attempts" in limited.text
            # Even the CORRECT token is refused while locked out.
            assert "wax_control_session" not in client.cookies
        finally:
            app.state.settings.control_plane_token = ""
            _login_limiter.reset()

    async def test_successful_login_clears_failures(self, client, app):
        from wax.runtime.control_plane import _login_limiter

        _login_limiter.reset()
        app.state.settings.control_plane_token = "op-token-1000"
        try:
            bad = await client.post("/control/login", data={"token": "wrong"})
            assert bad.status_code == 401
            good = await client.post("/control/login", data={"token": "op-token-1000"})
            assert good.status_code == 200
        finally:
            app.state.settings.control_plane_token = ""
            _login_limiter.reset()


class TestHandoffTimeline:
    async def test_timeline_shows_requested_and_awaiting_state(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        resp = await client.get(f"/control/handoffs/{handoff_id}")
        assert resp.status_code == 200
        assert "Activity" in resp.text
        assert "Requested" in resp.text
        assert "Awaiting the secret value" in resp.text
        # Not yet opened on first render? It IS opened by this very view —
        # the opened entry must exist with a timestamp.
        assert "Opened by an operator" in resp.text

    async def test_timeline_shows_completed_entry_after_submit(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)
        resp = await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": SECRET_TYPED_BY_HUMAN},
        )
        assert resp.status_code == 200
        assert "Completed — value encrypted &amp; stored" in resp.text
        assert "Awaiting the secret value" not in resp.text

    async def test_timeline_shows_expired_entry(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            handoff.challenge_expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await s.commit()
        resp = await client.get(f"/control/handoffs/{handoff_id}")
        assert resp.status_code == 200
        assert "Expired — challenge deadline passed" in resp.text


class TestDashboardLivePolling:
    async def test_poll_script_and_meta_refresh_both_rendered_while_open(self, client, app):
        principal_id = await _create_principal()
        await _create_handoff(principal_id)
        resp = await client.get("/control")
        assert 'http-equiv="refresh"' in resp.text  # no-JS fallback
        assert "meta[http-equiv=refresh]" in resp.text  # JS removes it
        assert "live-dot" in resp.text

    async def test_no_poll_script_without_open_handoffs(self, client, app):
        """No open handoffs → still no meta refresh, but the poller now
        ALWAYS renders and runs in heartbeat mode (JSON API polling with
        live stat-card sync) — the dashboard never goes fully dark."""
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)
        await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": "value-1"},
        )
        resp = await client.get("/control")
        assert 'http-equiv="refresh"' not in resp.text
        assert "setInterval" in resp.text  # heartbeat poller present
        assert '"/control/api/status"' in resp.text
        assert "heartbeat monitoring every 30s" in resp.text


class TestDashboardRuntimeActivity:
    async def test_quiet_state_without_services(self, client, app):
        principal_id = await _create_principal()
        await _create_handoff(principal_id)
        resp = await client.get("/control")
        assert "quiet — no activity recorded yet" in resp.text

    async def test_counters_render_when_services_present(self, client, app):
        if getattr(app.state, "services", None) is None:
            from wax.runtime.services import RuntimeServices

            app.state.services = RuntimeServices.build(app.state.settings)
        app.state.services.metrics.capability_invoked("success", "test.capability")
        app.state.services.metrics.terminal_executed(isolation="namespace")

        principal_id = await _create_principal()
        await _create_handoff(principal_id)
        resp = await client.get("/control")
        assert "Capability invocations" in resp.text
        assert "Terminal executions" in resp.text
        assert "since process start" in resp.text

    async def test_handoff_submission_increments_control_metric(self, client, app):
        if getattr(app.state, "services", None) is None:
            from wax.runtime.services import RuntimeServices

            app.state.services = RuntimeServices.build(app.state.settings)
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)
        # The metrics registry is a process-global singleton — assert on
        # the DELTA, never on an absolute value.
        before = app.state.services.metrics.snapshot_counters()
        resp = await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": SECRET_TYPED_BY_HUMAN},
        )
        assert resp.status_code == 200
        after = app.state.services.metrics.snapshot_counters()
        delta = after.get("control_handoffs_submitted_total", 0.0) - before.get(
            "control_handoffs_submitted_total", 0.0
        )
        assert delta == 1.0


class TestFooterVersion:
    async def test_footer_shows_version_on_all_pages(self, client, app):
        for url in ("/control", "/control/login"):
            resp = await client.get(url)
            assert "WAX Runtime v" in resp.text


# ---------------------------------------------------------------------------
# Round 4: pagination, CSV export, cancel, JSON status API, poll toasts
# ---------------------------------------------------------------------------


async def _seed_handoff_rows(
    principal_id: str, n: int, *, status: str = "completed", purpose_prefix: str = "Bulk"
) -> None:
    """Insert n handoff rows directly (fast bulk seeding for pagination)."""
    from ulid import ULID

    async with db_session() as s:
        for i in range(n):
            s.add(
                HumanHandoffRecord(
                    id=str(ULID()),
                    principal_id=principal_id,
                    purpose=f"{purpose_prefix} row {i:03d} — seeded for pagination",
                    origin_reference="https://bulk.example.com/job",
                    requested_actions_json={"handoff_kind": "secret", "actions": []},
                    status=status,
                    challenge_hash="seeded",
                    created_at_col=datetime.now(UTC) - timedelta(minutes=n - i),
                )
            )
        await s.commit()


class TestDashboardPagination:
    async def test_large_table_is_paginated(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_rows(principal_id, 55)

        page1 = await client.get("/control")
        assert page1.status_code == 200
        assert page1.text.count(">Bulk row") == 50
        assert "page 1 / 2" in page1.text
        assert "showing 1\u201350 of 55" in page1.text
        assert 'href="/control?status=all&amp;page=2"' in page1.text

        page2 = await client.get("/control", params={"page": 2})
        assert page2.status_code == 200
        assert page2.text.count(">Bulk row") == 5
        assert "showing 51\u201355 of 55" in page2.text
        assert 'href="/control?status=all&amp;page=1"' in page2.text

    async def test_out_of_range_pages_clamp(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_rows(principal_id, 120)

        for bad_page in ("0", "-3", "abc", ""):
            resp = await client.get("/control", params={"page": bad_page})
            assert resp.status_code == 200
            # non-numeric / non-positive pages clamp to the first page
            assert "page 1 / 3" in resp.text

        beyond = await client.get("/control", params={"page": "999"})
        assert beyond.status_code == 200
        # beyond the last page → clamped to the last page
        assert "page 3 / 3" in beyond.text

    async def test_pagination_respects_status_filter(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_rows(principal_id, 60, status="completed", purpose_prefix="Done")
        await _seed_handoff_rows(principal_id, 3, status="rejected", purpose_prefix="Refused")

        resp = await client.get("/control", params={"status": "failed"})
        assert resp.status_code == 200
        assert "showing 1\u20133 of 3 (failed)" in resp.text
        assert resp.text.count(">Refused row") == 3
        assert "Done row" not in resp.text

    async def test_pager_renders_ellipsis_for_many_pages(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_rows(principal_id, 500)

        resp = await client.get("/control", params={"page": 5})
        assert resp.status_code == 200
        assert "page 5 / 10" in resp.text
        assert 'class="ellipsis"' in resp.text
        assert 'aria-current="page">5<' in resp.text
        # first and last page always reachable
        assert 'href="/control?status=all&amp;page=1"' in resp.text
        assert 'href="/control?status=all&amp;page=10"' in resp.text

    async def test_no_pager_for_single_page(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_rows(principal_id, 3)
        resp = await client.get("/control")
        assert 'class="pager"' not in resp.text


class TestHandoffCsvExport:
    async def test_export_requires_authentication(self, client, app):
        app.state.settings.control_plane_token = "csv-token-1"
        try:
            resp = await client.get("/control/handoffs/export.csv", follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/control/login"
        finally:
            app.state.settings.control_plane_token = ""

    async def test_export_returns_metadata_only_csv(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_rows(principal_id, 3, purpose_prefix="Exported")
        resp = await client.get("/control/handoffs/export.csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "attachment" in resp.headers["content-disposition"]
        assert "wax-handoffs-" in resp.headers["content-disposition"]
        lines = resp.text.strip().splitlines()
        assert lines[0].split(",")[0] == "id"
        assert "Exported row" in resp.text
        # metadata-only: no secret columns, no ciphertext, no purpose secrets
        assert "secret_value" not in resp.text
        assert "ciphertext" not in resp.text

    async def test_export_honors_status_filter(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_rows(principal_id, 4, status="completed", purpose_prefix="Keep")
        await _seed_handoff_rows(principal_id, 2, status="rejected", purpose_prefix="Drop")
        resp = await client.get("/control/handoffs/export.csv", params={"status": "completed"})
        assert resp.status_code == 200
        assert "Keep row" in resp.text
        assert "Drop row" not in resp.text

    async def test_export_neutralizes_spreadsheet_formula_prefixes(self, client, app):
        principal_id = await _create_principal()
        from ulid import ULID

        async with db_session() as s:
            s.add(
                HumanHandoffRecord(
                    id=str(ULID()),
                    principal_id=principal_id,
                    purpose="=HYPERLINK('https://evil.example','click')",
                    requested_actions_json={"handoff_kind": "secret", "actions": []},
                    status="completed",
                    created_at_col=datetime.now(UTC),
                )
            )
            await s.commit()
        resp = await client.get("/control/handoffs/export.csv")
        assert resp.status_code == 200
        assert "'=HYPERLINK" in resp.text
        # the raw formula must never begin a cell untouched
        assert "\n=HYPERLINK" not in resp.text and "\r\n=HYPERLINK" not in resp.text


class TestHandoffCancel:
    async def test_danger_zone_renders_only_for_open_handoffs(self, client, app):
        principal_id = await _create_principal()
        open_id = await _create_handoff(principal_id)
        open_page = await client.get(f"/control/handoffs/{open_id}")
        assert "Danger zone" in open_page.text
        assert f'action="/control/handoffs/{open_id}/cancel"' in open_page.text

    async def test_cancel_with_valid_csrf_completes(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from_form(page.text, "/cancel")

        resp = await client.post(
            f"/control/handoffs/{handoff_id}/cancel",
            data={"csrf_token": csrf, "reason": "superseded by a newer request"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/control/handoffs/{handoff_id}?cancelled=1"

        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            assert handoff.status == "cancelled"
            assert handoff.failure_reason_safe == "superseded by a newer request"

        # the follow-up page shows the confirmation flash + timeline event
        # (the browser lands on the redirect target, query string included)
        detail = await client.get(f"/control/handoffs/{handoff_id}", params={"cancelled": "1"})
        assert "Handoff cancelled" in detail.text
        assert "Cancelled by an operator" in detail.text
        assert "superseded by a newer request" in detail.text

    async def test_cancel_rejects_wrong_scope_csrf(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        # token minted for the SUBMIT form must not cancel
        page = await client.get(f"/control/handoffs/{handoff_id}")
        submit_csrf = _csrf_from(page.text)  # first form in the page is the submit form

        resp = await client.post(
            f"/control/handoffs/{handoff_id}/cancel",
            data={"csrf_token": submit_csrf},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("?cancel=stale_token")

        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            # viewing the detail page marked it opened (still open, still uncancellable-by-that-token)
            assert handoff.status == "opened"

    async def test_cancel_rejects_terminal_handoff(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)
        await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": SECRET_TYPED_BY_HUMAN},
        )

        # completed handoffs no longer render the danger zone
        done_page = await client.get(f"/control/handoffs/{handoff_id}")
        assert "Danger zone" not in done_page.text

        from wax.runtime.control_plane import ControlPlaneAuth

        auth = ControlPlaneAuth(app.state.settings)
        resp = await client.post(
            f"/control/handoffs/{handoff_id}/cancel",
            data={"csrf_token": auth.make_csrf("cancel")},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("?cancel=error")

        async with db_session() as s:
            handoff = await s.get(HumanHandoffRecord, handoff_id)
            assert handoff.status == "completed"


class TestApiStatus:
    async def test_status_requires_authentication(self, client, app):
        app.state.settings.control_plane_token = "api-token-1"
        try:
            resp = await client.get("/control/api/status")
            assert resp.status_code == 401
            assert resp.json() == {"error": "not authenticated"}
        finally:
            app.state.settings.control_plane_token = ""

    async def test_status_returns_counts_and_metadata(self, client, app):
        principal_id = await _create_principal()
        await _create_handoff(principal_id)
        await _seed_handoff_rows(principal_id, 2)

        resp = await client.get("/control/api/status")
        assert resp.status_code == 200
        assert resp.headers["Cache-Control"] == "no-store"
        payload = resp.json()
        assert payload["status"] == "ok"
        assert payload["version"]
        assert payload["env"] == "development"
        assert payload["counts"]["handoffs_open"] == 1
        assert payload["counts"]["handoffs_completed"] == 2
        assert payload["counts"]["handoffs_total"] == 3

    async def test_status_works_with_bearer_token(self, client, app):
        app.state.settings.control_plane_token = "api-token-2"
        try:
            resp = await client.get(
                "/control/api/status", headers={"Authorization": "Bearer api-token-2"}
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"
        finally:
            app.state.settings.control_plane_token = ""

    async def test_status_never_leaks_purpose_text(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_rows(principal_id, 1, purpose_prefix="TOPSECRET-MARKER")
        resp = await client.get("/control/api/status")
        assert "TOPSECRET-MARKER" not in resp.text


class TestPollToasts:
    async def test_poll_state_marker_and_toast_code_rendered(self, client, app):
        principal_id = await _create_principal()
        await _create_handoff(principal_id)
        resp = await client.get("/control")
        assert resp.status_code == 200
        assert 'id="poll-state"' in resp.text
        assert 'data-open-count="1"' in resp.text
        # toast machinery ships with the poller
        assert "toast-stack" in resp.text
        assert "new handoff" in resp.text

    async def test_marker_and_heartbeat_rendered_without_open_handoffs(self, client, app):
        """The marker + poller now ALWAYS render; with zero open handoffs
        the poller runs in heartbeat mode (JSON API + stat-card sync)."""
        principal_id = await _create_principal()
        await _seed_handoff_rows(principal_id, 2)
        resp = await client.get("/control")
        assert resp.status_code == 200
        assert 'id="poll-state"' in resp.text
        assert 'data-open-count="0"' in resp.text
        assert '"/control/api/status"' in resp.text
        # stat cards carry data hooks the heartbeat updates live
        assert 'data-stat="open"' in resp.text
        assert 'data-card="open"' in resp.text

    async def test_stale_indicator_and_toast_deep_link_code_rendered(self, client, app):
        """Poll failures surface a reconnecting state; new-handoff toasts
        deep-link into the open filter."""
        principal_id = await _create_principal()
        await _create_handoff(principal_id)
        resp = await client.get("/control")
        assert "live updates paused" in resp.text  # stale indicator text
        assert "live-dot stale" in resp.text
        assert "Live updates resumed" in resp.text  # recovery toast
        assert "toast-action" in resp.text
        assert "/control?status=open" in resp.text  # deep-link target


# ---------------------------------------------------------------------------
# Round 5: date-range filters, metadata JSON panel, unified live poller
# ---------------------------------------------------------------------------


async def _seed_handoff_on(principal_id: str, *, days_ago: int, purpose: str) -> None:
    """One handoff row created exactly `days_ago` days before now."""
    from ulid import ULID

    async with db_session() as s:
        s.add(
            HumanHandoffRecord(
                id=str(ULID()),
                principal_id=principal_id,
                purpose=purpose,
                origin_reference="https://dates.example.com/job",
                requested_actions_json={"handoff_kind": "secret", "actions": []},
                status="completed",
                challenge_hash="seeded",
                created_at_col=datetime.now(UTC) - timedelta(days=days_ago),
            )
        )
        await s.commit()


class TestDateRangeFilter:
    async def test_since_excludes_older_rows(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_on(principal_id, days_ago=30, purpose="Old winter row")
        await _seed_handoff_on(principal_id, days_ago=1, purpose="Fresh recent row")

        cutoff = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
        resp = await client.get("/control", params={"since": cutoff})
        assert resp.status_code == 200
        assert "Fresh recent row" in resp.text
        assert "Old winter row" not in resp.text
        # the applied range is visible as a chip
        assert f"created {cutoff}" in resp.text
        assert "chip-clear" in resp.text

    async def test_until_excludes_newer_rows(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_on(principal_id, days_ago=30, purpose="Ancient archived row")
        await _seed_handoff_on(principal_id, days_ago=0, purpose="Today fresh row")

        cutoff = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
        resp = await client.get("/control", params={"until": cutoff})
        assert resp.status_code == 200
        assert "Ancient archived row" in resp.text
        assert "Today fresh row" not in resp.text

    async def test_until_day_is_inclusive(self, client, app):
        """until=2026-01-10 must include rows created ON that UTC day."""
        principal_id = await _create_principal()
        from ulid import ULID

        async with db_session() as s:
            s.add(
                HumanHandoffRecord(
                    id=str(ULID()),
                    principal_id=principal_id,
                    purpose="Boundary day row",
                    requested_actions_json={"handoff_kind": "secret", "actions": []},
                    status="completed",
                    challenge_hash="seeded",
                    created_at_col=datetime(2026, 1, 10, 23, 59, 0, tzinfo=UTC),
                )
            )
            await s.commit()

        resp = await client.get("/control", params={"until": "2026-01-10"})
        assert resp.status_code == 200
        assert "Boundary day row" in resp.text

        excluded = await client.get("/control", params={"until": "2026-01-09"})
        assert "Boundary day row" not in excluded.text

    async def test_since_and_until_combine_with_status_filter(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_on(principal_id, days_ago=1, purpose="Range completed row")
        await _seed_handoff_on(principal_id, days_ago=1, purpose="Range failed row")

        # mark the second row rejected
        async with db_session() as s:
            from sqlalchemy import select as _select

            row = (
                await s.execute(
                    _select(HumanHandoffRecord).where(
                        HumanHandoffRecord.purpose == "Range failed row"
                    )
                )
            ).scalar_one()
            row.status = "rejected"
            await s.commit()

        cutoff = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%d")
        resp = await client.get("/control", params={"status": "failed", "since": cutoff})
        assert resp.status_code == 200
        assert "Range failed row" in resp.text
        assert "Range completed row" not in resp.text
        assert "showing 1\u20131 of 1 (failed)" in resp.text

    async def test_invalid_date_returns_loud_400(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_on(principal_id, days_ago=1, purpose="Unrelated row")

        for bad in ("not-a-date", "2026-13-40", "20260101"):
            resp = await client.get("/control", params={"since": bad})
            assert resp.status_code == 400, f"{bad!r} must be rejected loudly"
            assert "Invalid since filter" in resp.text
            # the error page links back to the dashboard
            assert "Return to the dashboard" in resp.text

        # impossible calendar date on until too
        resp = await client.get("/control", params={"until": "2026-02-30"})
        assert resp.status_code == 400
        assert "not a real calendar date" in resp.text

    async def test_pager_preserves_date_filter(self, client, app):
        principal_id = await _create_principal()
        # 55 rows within the range → 2 pages, all inside the date window
        from ulid import ULID

        async with db_session() as s:
            for i in range(55):
                s.add(
                    HumanHandoffRecord(
                        id=str(ULID()),
                        principal_id=principal_id,
                        purpose=f"Dated bulk row {i:03d}",
                        requested_actions_json={"handoff_kind": "secret", "actions": []},
                        status="completed",
                        challenge_hash="seeded",
                        created_at_col=datetime.now(UTC) - timedelta(minutes=i),
                    )
                )
            await s.commit()

        cutoff = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
        resp = await client.get("/control", params={"since": cutoff})
        assert "page 1 / 2" in resp.text
        assert f"status=all&amp;since={cutoff}&amp;page=2" in resp.text
        page2 = await client.get("/control", params={"since": cutoff, "page": 2})
        assert "showing 51\u201355 of 55" in page2.text

    async def test_csv_export_honors_date_range(self, client, app):
        principal_id = await _create_principal()
        await _seed_handoff_on(principal_id, days_ago=30, purpose="CSV old row")
        await _seed_handoff_on(principal_id, days_ago=1, purpose="CSV new row")

        cutoff = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d")
        resp = await client.get("/control/handoffs/export.csv", params={"since": cutoff})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "CSV new row" in resp.text
        assert "CSV old row" not in resp.text

    async def test_csv_export_rejects_invalid_date(self, client, app):
        resp = await client.get("/control/handoffs/export.csv", params={"until": "bogus"})
        assert resp.status_code == 400
        assert "Invalid until filter" in resp.text


class TestMetadataJsonPanel:
    @staticmethod
    def _json_payload(html: str) -> dict:
        import html as _html
        import json as _json

        match = re.search(r'<pre class="json-pre">(.*?)</pre>', html, re.DOTALL)
        assert match, "json pre block must render"
        # the pre block is HTML-escaped (correctly — purposes can contain
        # markup); unescape before parsing
        return _json.loads(_html.unescape(match.group(1)))

    async def test_detail_renders_metadata_only_json(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        resp = await client.get(f"/control/handoffs/{handoff_id}")
        assert resp.status_code == 200
        assert "Metadata JSON" in resp.text
        payload = self._json_payload(resp.text)
        assert payload["id"] == handoff_id
        assert payload["purpose"].startswith("Deploy service")
        assert payload["status"] in ("pending", "opened")

    async def test_json_panel_never_contains_secret_material(self, client, app):
        principal_id = await _create_principal()
        handoff_id = await _create_handoff(principal_id)
        page = await client.get(f"/control/handoffs/{handoff_id}")
        csrf = _csrf_from(page.text)
        await client.post(
            f"/control/handoffs/{handoff_id}/submit",
            data={"csrf_token": csrf, "secret_value": SECRET_TYPED_BY_HUMAN},
        )
        resp = await client.get(f"/control/handoffs/{handoff_id}")
        assert SECRET_TYPED_BY_HUMAN not in resp.text  # never echoed, anywhere
        payload = self._json_payload(resp.text)
        # metadata-only: evidence carries opaque handles, never ciphertext
        import json as _json

        flat = _json.dumps(payload)
        assert "ciphertext" not in flat
        assert "challenge_hash" not in flat
        for key in payload.get("evidence", {}):
            assert key in ("material_type", "material_id", "grant_id")

    async def test_json_panel_escapes_html_in_purpose(self, client, app):
        """A purpose containing HTML must be escaped in the JSON view."""
        principal_id = await _create_principal()
        from ulid import ULID

        async with db_session() as s:
            s.add(
                HumanHandoffRecord(
                    id=str(ULID()),
                    principal_id=principal_id,
                    purpose="</pre><script>alert(1)</script>",
                    requested_actions_json={"handoff_kind": "secret", "actions": []},
                    status="completed",
                    challenge_hash="seeded",
                    created_at_col=datetime.now(UTC),
                )
            )
            await s.commit()
        resp = await client.get(f"/control/handoffs/{await _last_handoff_id()}")
        assert "<script>alert(1)</script>" not in resp.text
        assert "&lt;/pre&gt;&lt;script&gt;" in resp.text
        # after unescaping, the payload still carries the true purpose
        payload = self._json_payload(resp.text)
        assert payload["purpose"] == "</pre><script>alert(1)</script>"


async def _last_handoff_id() -> str:
    async with db_session() as s:
        from sqlalchemy import select as _select

        row = (
            (
                await s.execute(
                    _select(HumanHandoffRecord).order_by(
                        HumanHandoffRecord.created_at_col.desc().nullslast()
                    )
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        return row.id


# ---------------------------------------------------------------------------
# Round 6: operator search (?q=), authority-grants audit page, theme toggle
# ---------------------------------------------------------------------------


async def _seed_grant(
    principal_id: str,
    *,
    handle: str,
    status: str = "active",
    effect_class: str = "read_only",
    expires_in_hours: float = 24.0,
    last_used_hours_ago: float | None = None,
) -> str:
    """A material+grant pair with a FIXED handle (tests assert on it).

    status stays "active" with a past expires_at to model the live-expired
    case: no process has touched the row, yet the grant is dead.
    """
    from ulid import ULID

    now = datetime.now(UTC)
    material_id = str(ULID())
    async with db_session() as s:
        s.add(
            AuthorityMaterialRecord(
                id=material_id,
                principal_id=principal_id,
                material_type="opaque_secret",
                ciphertext="seeded-ciphertext",
                ciphertext_nonce="n" * 16,
                wrapped_data_key="k" * 32,
                wrapped_key_nonce="n" * 16,
                key_version=1,
                status="revoked" if status == "revoked" else "active",
                revoked_at=now if status == "revoked" else None,
                expires_at=now + timedelta(hours=expires_in_hours),
            )
        )
        s.add(
            AuthorityGrantRecord(
                id=str(ULID()),
                principal_id=principal_id,
                authority_material_id=material_id,
                handle=handle,
                allowed_actions_json={
                    "handoff_kind": "secret",
                    "actions": [
                        {
                            "description": "use the delegated credential",
                            "effect_class": effect_class,
                        }
                    ],
                },
                effect_class=effect_class,
                status=status,
                expires_at=now + timedelta(hours=expires_in_hours),
                revoked_at=now if status == "revoked" else None,
                last_used_at=(
                    now - timedelta(hours=last_used_hours_ago)
                    if last_used_hours_ago is not None
                    else None
                ),
            )
        )
        await s.commit()
    return handle


async def _grant_id_for(handle: str) -> str:
    async with db_session() as s:
        row = (
            await s.execute(
                select(AuthorityGrantRecord).where(AuthorityGrantRecord.handle == handle)
            )
        ).scalar_one()
        return row.id


class TestOperatorSearch:
    async def test_search_matches_purpose_substring(self, client, app):
        p = await _create_principal()
        await _seed_handoff_on(p, days_ago=0, purpose="Rotate the payment gateway key")
        await _seed_handoff_on(p, days_ago=0, purpose="Water the office plants")
        resp = await client.get("/control", params={"q": "gateway"})
        assert resp.status_code == 200
        assert "payment gateway key" in resp.text
        assert "office plants" not in resp.text
        assert 'value="gateway"' in resp.text  # input preserves the needle
        assert "search:" in resp.text  # active-filter chip

    async def test_search_matches_handoff_id_prefix(self, client, app):
        p = await _create_principal()
        await _seed_handoff_on(p, days_ago=0, purpose="Unique searchable purpose")
        handoff_id = await _last_handoff_id()
        resp = await client.get("/control", params={"q": handoff_id[:10]})
        assert resp.status_code == 200
        assert "Unique searchable purpose" in resp.text

    async def test_search_no_results_shows_search_empty_state(self, client, app):
        p = await _create_principal()
        await _seed_handoff_on(p, days_ago=0, purpose="Something ordinary")
        resp = await client.get("/control", params={"q": "zzz-no-such-thing"})
        assert resp.status_code == 200
        assert "No handoffs match the search" in resp.text

    async def test_search_escapes_like_metacharacters(self, client, app):
        """% and _ are literal: q="%" matches ONLY rows containing '%'."""
        p = await _create_principal()
        await _seed_handoff_on(p, days_ago=0, purpose="progress 100%_done today")
        await _seed_handoff_on(p, days_ago=0, purpose="plain purpose without specials")
        resp = await client.get("/control", params={"q": "%"})
        assert resp.status_code == 200
        assert "100%_done" in resp.text
        assert "plain purpose without specials" not in resp.text

    async def test_search_combines_with_status_filter(self, client, app):
        p = await _create_principal()
        await _seed_handoff_on(p, days_ago=0, purpose="alpha report job")
        await _seed_handoff_on(p, days_ago=0, purpose="beta report job")
        both = await client.get("/control", params={"q": "report", "status": "completed"})
        assert "alpha report job" in both.text and "beta report job" in both.text
        none = await client.get("/control", params={"q": "report", "status": "open"})
        assert "No handoffs match the search" in none.text

    async def test_pager_preserves_search(self, client, app):
        p = await _create_principal()
        await _seed_handoff_rows(p, 55, purpose_prefix="Searchable bulk")
        resp = await client.get("/control", params={"q": "Searchable", "page": 2})
        assert resp.status_code == 200
        assert "page 2" in resp.text
        assert "q=Searchable" in resp.text  # pager links carry the needle

    async def test_csv_export_honors_search(self, client, app):
        p = await _create_principal()
        await _seed_handoff_on(p, days_ago=0, purpose="needle in the haystack row")
        await _seed_handoff_on(p, days_ago=0, purpose="unrelated row")
        resp = await client.get("/control/handoffs/export.csv", params={"q": "needle"})
        assert resp.status_code == 200
        assert "needle in the haystack row" in resp.text
        assert "unrelated row" not in resp.text

    async def test_search_needle_is_length_capped(self, client, app):
        p = await _create_principal()
        await _seed_handoff_on(p, days_ago=0, purpose="ordinary row")
        resp = await client.get("/control", params={"q": "x" * 500})
        assert resp.status_code == 200  # capped, not an error


class TestAuthorityGrantsPage:
    async def test_grants_page_renders_rows_and_counts(self, client, app):
        p = await _create_principal()
        await _seed_grant(p, handle="A" * 40, effect_class="read_only")
        await _seed_grant(p, handle="B" * 40, effect_class="destructive")
        await _seed_grant(p, handle="C" * 40, status="revoked")
        resp = await client.get("/control/grants")
        assert resp.status_code == 200
        assert "Authority grants" in resp.text
        # status tabs render with the filter list
        assert 'role="tab"' in resp.text
        # revoke buttons appear only for active grants (2 of them), keyed
        # by RECORD id — the authority handle itself never appears in URLs
        assert resp.text.count("/revoke") == 2
        # handles are NEVER fully rendered (the handle is the authority
        # token the intelligence holds) — abbreviated form only
        assert "A" * 40 not in resp.text
        assert "A" * 10 + "…" in resp.text
        assert "destructive" in resp.text
        # last_used shows "never" when untouched
        assert "never" in resp.text

    async def test_grants_page_filter_tabs(self, client, app):
        p = await _create_principal()
        await _seed_grant(p, handle="A" * 40)
        await _seed_grant(p, handle="C" * 40, status="revoked")
        revoked = await client.get("/control/grants", params={"status": "revoked"})
        assert revoked.status_code == 200
        assert "A" * 10 + "…" not in revoked.text
        assert "C" * 10 + "…" in revoked.text

    async def test_expired_is_computed_live(self, client, app):
        """A grant whose row still says active but whose expires_at has
        passed renders as expired — and gets no revoke button."""
        p = await _create_principal()
        await _seed_grant(p, handle="D" * 40, expires_in_hours=-5.0)
        resp = await client.get("/control/grants")
        assert "D" * 10 + "…" in resp.text
        assert ">expired</span>" in resp.text
        # the expired grant has no revoke form
        assert "/revoke" not in resp.text

    async def test_expiring_soon_hint(self, client, app):
        p = await _create_principal()
        await _seed_grant(p, handle="E" * 40, expires_in_hours=2.0)
        resp = await client.get("/control/grants")
        assert "expires soon" in resp.text
        # future expiry reads as "in Nh" — never clamped to "just now",
        # which would mislead an operator about when the grant dies
        assert "in 2h" in resp.text

    async def test_revoke_revokes_grant_and_material(self, client, app):
        p = await _create_principal()
        handle = await _seed_grant(p, handle="F" * 40)
        grant_id = await _grant_id_for(handle)
        page = await client.get("/control/grants")
        csrf = _csrf_from_form(page.text, "/revoke")
        resp = await client.post(f"/control/grants/{grant_id}/revoke", data={"csrf_token": csrf})
        assert resp.status_code == 200  # followed redirect to ?revoked=1
        assert "Authority grant revoked" in resp.text
        # the handle never leaks into the operator HTML
        assert handle not in page.text
        async with db_session() as s:
            grant = (
                await s.execute(
                    select(AuthorityGrantRecord).where(AuthorityGrantRecord.handle == handle)
                )
            ).scalar_one()
            assert grant.status == "revoked"
            assert grant.revoked_at is not None
            material = await s.get(AuthorityMaterialRecord, grant.authority_material_id)
            assert material is not None
            assert material.status == "revoked"
            assert material.revoked_at is not None

    async def test_revoke_rejects_foreign_scope_csrf(self, client, app):
        """A token minted for the submit form is worthless here (scoped
        CSRF): no revocation happens, the operator gets a loud error."""
        p = await _create_principal()
        handle = await _seed_grant(p, handle="G" * 40)
        grant_id = await _grant_id_for(handle)
        handoff_id = await _create_handoff(p)
        detail = await client.get(f"/control/handoffs/{handoff_id}")
        handoff_csrf = _csrf_from_form(detail.text, "/submit")
        resp = await client.post(
            f"/control/grants/{grant_id}/revoke", data={"csrf_token": handoff_csrf}
        )
        assert "revoke confirmation has expired" in resp.text
        async with db_session() as s:
            grant = (
                await s.execute(
                    select(AuthorityGrantRecord).where(AuthorityGrantRecord.handle == handle)
                )
            ).scalar_one()
            assert grant.status == "active"

    async def test_revoke_unknown_handle_shows_error(self, client, app):
        p = await _create_principal()
        # one live grant exists so the page carries a revoke form (the
        # token source); the POST itself targets a nonexistent id
        await _seed_grant(p, handle="J" * 40)
        page = await client.get("/control/grants")
        csrf = _csrf_from_form(page.text, "/revoke")
        resp = await client.post(
            "/control/grants/NOPE" + "Z" * 23 + "/revoke", data={"csrf_token": csrf}
        )
        assert "No grant with that handle exists" in resp.text

    async def test_grants_reachable_from_dashboard(self, client, app):
        p = await _create_principal()
        await _seed_grant(p, handle="H" * 40)
        dash = await client.get("/control")
        assert 'href="/control/grants"' in dash.text
        grants = await client.get("/control/grants")
        assert 'href="/control/grants"' in grants.text
        assert 'aria-current="page"' in grants.text  # nav active state


class TestThemeToggle:
    async def test_toggle_and_bootstrap_render_on_dashboard_and_login(self, client, app):
        p = await _create_principal()
        await _seed_handoff_on(p, days_ago=0, purpose="theme probe row")
        dash = await client.get("/control")
        assert 'id="theme-toggle"' in dash.text
        assert "wax-theme" in dash.text  # bootstrap + persistence script
        assert "data-theme" in dash.text
        login = await client.get("/control/login")
        assert 'id="theme-toggle"' in login.text
        assert "wax-theme" in login.text
