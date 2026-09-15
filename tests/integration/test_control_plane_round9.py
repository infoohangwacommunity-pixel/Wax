"""Round-9 control-plane work: size-based audit retention bound, the
dashboard's open-card staleness figure, upgraded grants expiring badge,
and the heartbeat pill's grant awareness.

Pinned honestly:
- WAX_AUDIT_MAX_ROWS is OPT-IN (0 = off): the ledger is append-only
  (INV-06) and rows are only ever removed by an explicitly configured
  bound — the OLDEST rows beyond the bound go first. The age window
  (WAX_AUDIT_RETENTION_DAYS) and the row bound compose; each reports
  its own counter.
- "action needed" on the open card carries an honest staleness figure
  ("oldest waiting 2h") — server-rendered AND heartbeat-maintained via
  the status API's handoffs_open_oldest_seconds.
- A grant that dies on its own within 6h shows a precise amber badge
  ("expires in 2h") instead of a vague "expires soon" hint.
- The heartbeat pill distinguishes "all quiet" from "quiet · N live
  grants" — grants are state an operator may want to look at even when
  no handoff is waiting.
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
from wax.state.authority_broker_models import (
    AuthorityGrantRecord,
    AuthorityMaterialRecord,
    HumanHandoffRecord,
)
from wax.state.engine import db_session, dispose_engine, init_engine
from wax.state.models import Base

pytestmark = pytest.mark.integration


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
                origin_reference="https://example.com/round9",
                requested_actions=[{"description": "store a key", "effect_class": "write"}],
                handoff_kind="secret",
                instructions_text="Paste the key.",
            ),
        )
        await s.commit()
        return result.handoff_ref


async def _age_open_handoff(handoff_id: str, *, hours: float) -> None:
    """Backdate a handoff's creation so staleness figures are testable.

    Uses `created_at_col` — the model's explicit-override timestamp that
    every dashboard ordering/aging expression coalesces FIRST; the mixin
    `created_at` stays the broker's bookkeeping value."""
    async with db_session() as s:
        row = await s.get(HumanHandoffRecord, handoff_id)
        assert row is not None
        row.created_at_col = datetime.now(UTC) - timedelta(hours=hours)
        await s.commit()


async def _seed_audit_event(*, event_kind: str, payload: dict, age_days: float = 0.0) -> str:
    row = AuditEvent(
        id=str(ULID()),
        actor_principal_id=None,
        actor_kind="system",
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


async def _seed_grant(
    principal_id: str,
    *,
    handle: str,
    status: str = "active",
    effect_class: str = "read_only",
    expires_in_hours: float = 24.0,
) -> str:
    """A material+grant pair with a FIXED handle (tests assert on it)."""
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
                last_used_at=None,
            )
        )
        await s.commit()
    return handle


class TestAuditMaxRowsSweep:
    """The size-based sibling of the age window: oldest rows beyond an
    explicitly configured bound go first; 0 (default) never deletes."""

    async def test_bound_off_never_deletes(self, db):
        await _seed_audit_event(event_kind="control.login", payload={}, age_days=400)
        await _seed_audit_event(event_kind="control.login", payload={})

        results = await run_maintenance_pass(db)
        assert results["audit_events_overflow_pruned"] == 0
        assert len(await _audit_ids()) == 2

    async def test_bound_prunes_oldest_beyond_limit(self, db):
        from wax.core.config import settings_for_testing

        settings = settings_for_testing(audit_max_rows=3)
        old1 = await _seed_audit_event(event_kind="control.login", payload={}, age_days=5)
        old2 = await _seed_audit_event(event_kind="control.login", payload={}, age_days=4)
        mid = await _seed_audit_event(event_kind="control.login", payload={}, age_days=2)
        fresh1 = await _seed_audit_event(event_kind="control.login", payload={}, age_days=1)
        fresh2 = await _seed_audit_event(event_kind="control.login", payload={})

        results = await run_maintenance_pass(settings)
        assert results["audit_events_overflow_pruned"] == 2
        assert results["audit_events_pruned"] == 0  # the age gate stayed off

        remaining = await _audit_ids()
        assert {old1, old2}.isdisjoint(remaining)  # the OLDEST went first
        assert {mid, fresh1, fresh2} <= remaining  # exactly the bound remains

    async def test_bound_under_limit_prunes_nothing(self, db):
        from wax.core.config import settings_for_testing

        settings = settings_for_testing(audit_max_rows=10)
        await _seed_audit_event(event_kind="control.login", payload={})
        await _seed_audit_event(event_kind="control.login", payload={})

        results = await run_maintenance_pass(settings)
        assert results["audit_events_overflow_pruned"] == 0
        assert len(await _audit_ids()) == 2

    async def test_age_window_and_bound_compose(self, db):
        from wax.core.config import settings_for_testing

        settings = settings_for_testing(audit_retention_days=7, audit_max_rows=2)
        ancient = await _seed_audit_event(event_kind="system.blob_gc", payload={}, age_days=30)
        old = await _seed_audit_event(event_kind="control.login", payload={}, age_days=2)
        fresh = await _seed_audit_event(event_kind="control.login", payload={}, age_days=1)

        results = await run_maintenance_pass(settings)
        assert results["audit_events_pruned"] == 1  # the 30d-old row by age
        assert results["audit_events_overflow_pruned"] == 0  # 2 remain, bound is 2
        remaining = await _audit_ids()
        assert ancient not in remaining
        assert {old, fresh} <= remaining  # exactly the 2-row bound remains


class TestDashboardStaleness:
    async def test_open_card_shows_oldest_age(self, client):
        p = await _create_principal()
        hid = await _create_handoff(p, purpose="Round9 staleness probe")
        await _age_open_handoff(hid, hours=2)

        resp = await client.get("/control")
        assert resp.status_code == 200
        assert "oldest waiting 2h ago" in resp.text

        status = await client.get("/control/api/status")
        assert status.status_code == 200
        payload = status.json()
        oldest = payload["counts"]["handoffs_open_oldest_seconds"]
        assert oldest is not None and 7000 <= oldest <= 7400

    async def test_no_aging_when_nothing_open(self, client):
        resp = await client.get("/control")
        assert resp.status_code == 200
        assert "nothing waiting" in resp.text
        # no FIGURE is rendered (the string "oldest waiting" also appears
        # in the poller script, so absence is pinned via the API + the
        # quiet sub-text)
        assert "nothing waiting" in resp.text

        status = await client.get("/control/api/status")
        assert status.json()["counts"]["handoffs_open_oldest_seconds"] is None

    async def test_aging_figure_tracks_the_actual_oldest(self, client):
        p = await _create_principal()
        await _create_handoff(p, purpose="Round9 fresh probe")
        stale_id = await _create_handoff(p, purpose="Round9 stale probe")
        await _age_open_handoff(stale_id, hours=26)

        status = await client.get("/control/api/status")
        oldest = status.json()["counts"]["handoffs_open_oldest_seconds"]
        assert oldest is not None and 90000 <= oldest <= 94000  # 26h in seconds

        resp = await client.get("/control")
        assert "oldest waiting 1d ago" in resp.text


class TestGrantsExpiringBadge:
    async def test_soon_expiry_shows_precise_amber_badge(self, client):
        p = await _create_principal()
        await _seed_grant(p, handle="E" * 40, expires_in_hours=2)
        await _seed_grant(p, handle="F" * 40, expires_in_hours=48)

        resp = await client.get("/control/grants")
        assert resp.status_code == 200
        # the 2h grant: an amber badge with the precise future time
        assert 'badge expiring" title=' in resp.text
        assert "expires in 2h" in resp.text
        # the 48h grant is NOT "soon" — exactly one expiring badge renders
        assert resp.text.count("badge expiring") == 1

    async def test_no_expiring_badge_when_far_from_expiry(self, client):
        p = await _create_principal()
        await _seed_grant(p, handle="G" * 40, expires_in_hours=72)

        resp = await client.get("/control/grants")
        assert resp.status_code == 200
        assert "badge expiring" not in resp.text
        assert "badge active" in resp.text  # the grant still renders normally


class TestHeartbeatPillGrants:
    async def test_script_distinguishes_quiet_states(self, client):
        resp = await client.get("/control/audit")
        assert resp.status_code == 200
        assert "all quiet" in resp.text  # no handoffs, no grants
        assert "live grant" in resp.text  # grants-aware quiet state

    async def test_status_payload_carries_grant_count(self, client):
        p = await _create_principal()
        await _seed_grant(p, handle="H" * 40, expires_in_hours=24)

        status = await client.get("/control/api/status")
        assert status.json()["counts"]["authority_grants_active"] == 1


class TestRetentionWarningRowsBound:
    def test_row_bound_alone_suppresses_warning(self):
        from wax.runtime.control_plane import _retention_warning

        assert _retention_warning(10**7, 0, max_rows=100_000) is None

    def test_no_policy_at_all_still_warns(self):
        from wax.runtime.control_plane import _AUDIT_WARN_ROWS, _retention_warning

        msg = _retention_warning(_AUDIT_WARN_ROWS, 0, max_rows=0)
        assert msg is not None
        assert "WAX_AUDIT_MAX_ROWS" in msg

    async def test_header_shows_rows_bound_chip(self, db):
        from wax.core.config import settings_for_testing

        settings = settings_for_testing(audit_max_rows=250_000)
        settings.__dict__["database_url"] = "sqlite+aiosqlite:///:memory:"
        application = create_app(settings=settings)
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/control/audit")
        assert resp.status_code == 200
        assert "250000 rows max" in resp.text
        assert "off (kept forever)" not in resp.text
