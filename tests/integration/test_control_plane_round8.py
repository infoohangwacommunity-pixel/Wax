"""Round-8 control-plane work: audit-ledger retention, audit tab family
counts, heartbeat pills on the ledger pages, and the storage card's
last-GC fact.

Pinned honestly:
- Retention is OPT-IN (WAX_AUDIT_RETENTION_DAYS): the ledger is
  append-only (INV-06) and the application never deletes history
  silently — the scheduled sweep only removes rows older than an
  explicitly configured window, and the gate off (default) deletes
  nothing at all.
- The audit page states the retention policy in its header and warns
  when the ledger is unbounded AND large — a misconfigured deployment
  must not become a silent storage leak.
- Kind tabs carry per-family counts from ONE grouped query.
- Audit/grants pages get a heartbeat pill fed by /control/api/status;
  the dashboard does NOT render it (its own two-mode poller would
  double-poll).
- The dashboard storage card shows the most recent GC fact from the
  audit ledger, labeled by actor (scheduled system sweep vs operator).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from ulid import ULID

from wax.authority.seed import seed_builtin_roles
from wax.identity.repository import PrincipalRepository
from wax.runtime.app import create_app
from wax.runtime.authority.broker import AuthorityBroker
from wax.runtime.authority.contracts import AuthorityRequest
from wax.runtime.maintenance import run_maintenance_pass
from wax.state.audit_models import AuditEvent
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base

pytestmark = pytest.mark.integration

SECRET_TYPED_BY_HUMAN = "round8-operator-secret-DO-NOT-ECHO-4b7e"


@pytest.fixture
async def db():
    from wax.core.config import settings_for_testing

    settings = settings_for_testing()
    settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
    init_engine(settings)
    engine = init_engine.__globals__["_engine"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db_session() as s:
        await seed_builtin_roles(s)
        await s.commit()
    yield settings
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


async def _create_principal(*, phone: str = "1234567890") -> str:
    from wax.authority.seed import ensure_principal_role

    async with db_session() as s:
        repo = PrincipalRepository(s)
        principal = await repo.create_principal(display_name="Operator")
        await repo.add_credential(
            principal.id, kind="whatsapp_phone", value=phone, is_verified=True
        )
        await ensure_principal_role(s, principal.id, "admin")
        await s.commit()
        return principal.id


async def _create_handoff(principal_id: str, *, purpose: str) -> str:
    async with db_session() as s:
        broker = AuthorityBroker()
        result = await broker.request_authority(
            s,
            principal_id=principal_id,
            request=AuthorityRequest(
                purpose=purpose,
                origin_reference="https://example.com/round8",
                requested_actions=[{"description": "store a key", "effect_class": "write"}],
                handoff_kind="secret",
                instructions_text="Paste the key.",
            ),
        )
        await s.commit()
        return result.handoff_ref


async def _last_open_handoff_id() -> str:
    from wax.state.authority_broker_models import HumanHandoffRecord

    async with db_session() as s:
        row = (
            (
                await s.execute(
                    select(HumanHandoffRecord)
                    .where(HumanHandoffRecord.status == "pending")
                    .order_by(HumanHandoffRecord.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        return row.id


async def _submit_handoff(client: AsyncClient, handoff_id: str) -> None:
    import re

    page = await client.get(f"/control/handoffs/{handoff_id}")
    form_re = re.compile(r'<form[^>]*action="[^"]*/submit[^"]*"[^>]*>(.*?)</form>', re.DOTALL)
    match = form_re.search(page.text)
    assert match, "submit form must exist"
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', match.group(1)).group(1)
    resp = await client.post(
        f"/control/handoffs/{handoff_id}/submit",
        data={"csrf_token": csrf, "secret_value": SECRET_TYPED_BY_HUMAN},
    )
    assert resp.status_code == 200


async def _seed_audit_event(
    *, event_kind: str, payload: dict, age_days: float = 0.0, actor_kind: str = "system"
) -> str:
    """Direct row insert — record_audit_event always stamps now(), but
    retention and last-GC need controlled timestamps."""
    row = AuditEvent(
        id=str(ULID()),
        actor_principal_id=None,
        actor_kind=actor_kind,
        event_kind=event_kind,
        outcome="success",
        payload=payload,
    )
    if age_days:
        row.created_at = datetime.now(UTC) - timedelta(days=age_days)
        row.updated_at = row.created_at
    async with db_session() as s:
        s.add(row)
        await s.commit()
        return row.id


async def _audit_ids() -> set[str]:
    async with db_session() as s:
        rows = (await s.execute(select(AuditEvent))).scalars().all()
        return {r.id for r in rows}


class TestAuditRetentionSweep:
    """The scheduled sweep deletes only past an EXPLICIT window; the
    default gate (0 days) never deletes anything."""

    async def test_gate_off_never_deletes(self, db):
        await _seed_audit_event(event_kind="system.blob_gc", payload={"removed": 1}, age_days=400)
        await _seed_audit_event(event_kind="control.login", payload={"ip": "10.0.0.1"})

        results = await run_maintenance_pass(db)
        assert results["audit_events_pruned"] == 0
        assert len(await _audit_ids()) == 2  # old row survives: no policy configured

    async def test_sweep_removes_only_events_older_than_window(self, db):
        from wax.core.config import settings_for_testing

        settings = settings_for_testing(audit_retention_days=7)
        old_id = await _seed_audit_event(
            event_kind="system.blob_gc", payload={"removed": 1}, age_days=30
        )
        fresh_id = await _seed_audit_event(event_kind="control.login", payload={"ip": "10.0.0.2"})
        boundary_id = await _seed_audit_event(
            event_kind="control.login", payload={"ip": "10.0.0.3"}, age_days=6.9
        )

        results = await run_maintenance_pass(settings)
        assert results["audit_events_pruned"] == 1

        remaining = await _audit_ids()
        assert old_id not in remaining
        assert {fresh_id, boundary_id} <= remaining

    async def test_sweep_reports_zero_when_ledger_empty(self, db):
        from wax.core.config import settings_for_testing

        results = await run_maintenance_pass(settings_for_testing(audit_retention_days=7))
        assert results["audit_events_pruned"] == 0


class TestAuditPageRetentionAndCounts:
    async def test_header_shows_retention_off_by_default(self, client):
        resp = await client.get("/control/audit")
        assert resp.status_code == 200
        assert "retention" in resp.text
        assert "off (kept forever)" in resp.text
        # no growth warning on a small ledger (message text, not the CSS
        # class name — the stylesheet itself contains 'warn-note')
        assert "no retention policy is configured" not in resp.text

    async def test_header_shows_configured_policy(self, db):
        from wax.core.config import settings_for_testing

        settings = settings_for_testing(audit_retention_days=90)
        settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
        application = create_app(settings=settings)
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/control/audit")
        assert resp.status_code == 200
        assert "90d" in resp.text
        assert "off (kept forever)" not in resp.text

    async def test_warn_note_renders_when_unbounded_and_large(self, client, monkeypatch):
        from wax.runtime import control_plane as cp

        # one real event so the page has content; then lower the render
        # threshold so the warning path is reachable without 50k rows
        await _seed_audit_event(event_kind="control.login", payload={"ip": "10.0.0.4"})
        monkeypatch.setattr(cp, "_AUDIT_WARN_ROWS", 1)
        resp = await client.get("/control/audit")
        assert "no retention policy is configured" in resp.text
        assert "WAX_AUDIT_RETENTION_DAYS" in resp.text

    async def test_kind_tabs_render_family_counts(self, client):
        principal_id = await _create_principal()
        await _create_handoff(principal_id, purpose="Round8 counts probe A")
        await _create_handoff(principal_id, purpose="Round8 counts probe B")
        await _submit_handoff(client, await _last_open_handoff_id())

        resp = await client.get("/control/audit")
        assert resp.status_code == 200
        # every tab carries a count pill
        assert resp.text.count('title="Events in this family (all pages)"') == 5
        # the grouped counts must be internally consistent
        from wax.runtime.control_plane import _audit_family_counts

        async with db_session() as s:
            counts = await _audit_family_counts(s)
        # one submit → exactly one handoff-family event; dev-open writes
        # no login events; "all" never undercounts a family
        assert counts["handoffs"] == 1
        assert counts["login"] == 0
        assert counts["all"] >= counts["handoffs"]

    async def test_family_counts_render_on_filtered_views_too(self, client):
        principal_id = await _create_principal()
        await _create_handoff(principal_id, purpose="Round8 filter probe")
        await _submit_handoff(client, await _last_open_handoff_id())

        resp = await client.get("/control/audit", params={"kind": "maintenance"})
        assert resp.status_code == 200
        # counts are per-family across ALL pages, independent of the filter
        assert 'title="Events in this family (all pages)"' in resp.text

    async def test_logout_belongs_to_the_login_family(self, client):
        """A sign-out has no other home: it must count under the login
        tab (both the count pill and the kind filter)."""
        principal_id = await _create_principal()
        await _create_handoff(principal_id, purpose="Round8 logout probe")
        await _seed_audit_event(event_kind="control.logout", payload={}, actor_kind="human")

        from wax.runtime.control_plane import _audit_family_counts

        async with db_session() as s:
            counts = await _audit_family_counts(s)
        assert counts["login"] >= 1

        resp = await client.get("/control/audit", params={"kind": "login"})
        assert "Signed out" in resp.text  # the logout event is reachable via the tab


class TestHeartbeatPills:
    async def test_audit_and_grants_have_heartbeat_dashboard_does_not(self, client):
        principal_id = await _create_principal()
        await _create_handoff(principal_id, purpose="Round8 heartbeat probe")

        # script-body markers unique to the heartbeat script (the .hb-pill
        # CSS class is in base.html's stylesheet on EVERY page — only the
        # script is page-conditional; "reconnecting" also appears in the
        # dashboard's own poller so it proves nothing here)
        script_markers = ("all quiet", "handoffs waiting")

        audit = await client.get("/control/audit")
        for marker in script_markers:
            assert marker in audit.text
        assert '/control/api/status"' in audit.text

        grants = await client.get("/control/grants")
        for marker in script_markers:
            assert marker in grants.text

        dash = await client.get("/control")
        for marker in script_markers:
            assert marker not in dash.text  # dashboard has its own poller
        assert "poll-state" in dash.text  # ...and it is still there

        detail = await client.get(f"/control/handoffs/{await _last_open_handoff_id()}")
        for marker in script_markers:
            assert marker not in detail.text  # detail page: neither poller

    async def test_csv_export_has_no_heartbeat(self, client):
        resp = await client.get("/control/audit/export.csv")
        assert resp.status_code == 200
        assert "all quiet" not in resp.text


class TestDevOpenSignOutConsistency:
    """Bug fixed in round 8: only the dashboard passed `dev_open` to its
    template, so in dev-open mode (no token, no session) the audit,
    grants and detail pages still rendered a "Sign out" button — a form
    that could only ever no-op. Every page that extends base.html must
    agree on whether an auth session exists."""

    async def test_dev_open_pages_hide_sign_out(self, client):
        for path in (
            "/control",
            "/control/audit",
            "/control/grants",
            "/control/login",
        ):
            resp = await client.get(path)
            assert resp.status_code == 200
            assert "Sign out" not in resp.text, f"{path} showed Sign out in dev-open mode"

    async def test_token_configured_pages_show_sign_out(self, db):
        from wax.core.config import settings_for_testing

        settings = settings_for_testing()
        settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
        settings.control_plane_token = "round8-token"
        application = create_app(settings=settings)
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            ok = await ac.post("/control/login", data={"token": "round8-token"})
            assert ok.status_code in (200, 303)  # login may redirect to the dashboard
            for path in ("/control", "/control/audit", "/control/grants"):
                resp = await ac.get(path)
                assert resp.status_code == 200
                assert "Sign out" in resp.text, f"{path} hid Sign out despite a live session"


class TestDashboardLastGc:
    async def test_no_line_when_gc_never_ran(self, client):
        resp = await client.get("/control")
        assert resp.status_code == 200
        assert "last GC removed" not in resp.text

    async def test_scheduled_gc_fact_renders(self, client):
        await _seed_audit_event(
            event_kind="system.blob_gc",
            payload={"removed": 4, "reclaimed_bytes": 4096},
            actor_kind="system",
        )
        resp = await client.get("/control")
        assert "last GC removed 4 blobs (4.0 KB)" in resp.text
        assert ", scheduled" in resp.text

    async def test_operator_gc_fact_renders_with_actor_label(self, client):
        await _seed_audit_event(
            event_kind="control.blob_gc",
            payload={"removed": 1, "reclaimed_bytes": 512},
            actor_kind="human",
        )
        resp = await client.get("/control")
        assert "last GC removed 1 blob (0.5 KB)" in resp.text
        assert ", operator" in resp.text

    async def test_most_recent_gc_wins(self, client):
        await _seed_audit_event(
            event_kind="system.blob_gc",
            payload={"removed": 9, "reclaimed_bytes": 9216},
            age_days=1.0,  # a day old
        )
        await _seed_audit_event(
            event_kind="control.blob_gc",
            payload={"removed": 2, "reclaimed_bytes": 1024},
            actor_kind="human",  # seeded now → newer → wins
        )
        resp = await client.get("/control")
        assert "last GC removed 2 blobs (1.0 KB)" in resp.text

    async def test_zero_removal_is_still_honest(self, client):
        await _seed_audit_event(
            event_kind="system.blob_gc", payload={"removed": 0, "reclaimed_bytes": 0}
        )
        resp = await client.get("/control")
        assert "last GC removed 0 blobs (0.0 KB)" in resp.text


class TestRetentionWarningHelper:
    def test_small_ledger_no_warning(self):
        from wax.runtime.control_plane import _retention_warning

        assert _retention_warning(100, 0) is None

    def test_policy_configured_no_warning_even_when_huge(self):
        from wax.runtime.control_plane import _retention_warning

        assert _retention_warning(10**7, 30) is None

    def test_unbounded_and_large_warns_with_setting_name(self):
        from wax.runtime.control_plane import _AUDIT_WARN_ROWS, _retention_warning

        msg = _retention_warning(_AUDIT_WARN_ROWS, 0)
        assert msg is not None
        assert "WAX_AUDIT_RETENTION_DAYS" in msg
        assert f"{_AUDIT_WARN_ROWS:,}" in msg
