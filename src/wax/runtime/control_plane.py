"""Secure control plane — the server-rendered same-origin dashboard.

ADR-0048 specified this surface but it was never implemented: the
authority broker's `submit_handoff` had NO human-reachable entrypoint,
so a handoff could be created by the intelligence but never completed by
a human. This module is that missing HTTP layer.

Surface (all server-rendered HTML, same-origin, no external assets):

- GET  /control                          dashboard home (handoff overview,
                                         paginated, ?status= + ?since=/?until=
                                         UTC-day filters + ?q= search)
- GET  /control/grants                   authority-grant audit view
                                         (?status= filter)
- GET  /control/grants/export.csv        metadata-only grants CSV (NEVER the
                                         handle — it is the live token)
- POST /control/grants/{handle}/revoke   operator revokes a live grant
- GET  /control/audit                    operator audit trail (kind tabs +
                                         metadata-only ?q= search)
- GET  /control/audit/export.csv         metadata-only audit CSV (same
                                         filters; spreadsheet-formula safe)
- GET  /control/login                    operator login
- POST /control/login                    verify token → session cookie
- POST /control/logout                   clear session
- GET  /control/handoffs/{id}            handoff detail (metadata only, links
                                         its own audit entries)
- POST /control/handoffs/{id}/submit     submit the secret (encrypted
                                         immediately, never echoed)
- POST /control/handoffs/{id}/cancel     operator cancels an open handoff
                                         (CSRF-protected, wakes waiting work)
- POST /control/handoffs/cancel-bulk     bulk-cancel open handoffs (capped)
- GET  /control/handoffs/export.csv      metadata-only CSV export (same auth;
                                         spreadsheet-formula sanitized)
- POST /control/maintenance/gc           run blob-store garbage collection
                                         (operator-only, CSRF-protected)
- GET  /control/api/status               read-only JSON status endpoint (same
                                         auth) for external monitoring

Security posture (per ADR-0048 "Control plane"):
- Authentication: requires WAX_CONTROL_PLANE_TOKEN (bearer header,
  X-Control-Token header, ?token=, or the HttpOnly SameSite=Strict
  session cookie issued after login). Without a token the dashboard is
  enabled ONLY in development (with a loud banner) and REFUSES to serve
  in production (503) — misconfiguration must be loud, never silent.
- CSRF: signed, expiring, handoff-bound token embedded in every form and
  verified on submit (HMAC-SHA256 over expiry + nonce).
- Brute force: failed logins are rate limited per client IP (in-memory
  sliding window) — a misconfigured deployment must not become a token
  oracle.
- Cache-Control: no-store on every response; the secret is NEVER echoed,
  displayed, or logged after submission.
- No external resources: styles are inline; nothing leaks the operator's
  IP to a CDN.

The dashboard is an INTERFACE adapter — it lives in wax.runtime, never
imports the bridge or wax.interfaces (INV-02), and knows nothing about
domains, education, or WhatsApp.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import re
import threading
import time
from collections import defaultdict, deque
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, func, select

from wax import __version__ as _wax_version
from wax.runtime.authority.contracts import (
    AuthorityStatus,
    HandoffStatus,
    MaterialType,
)
from wax.runtime.logging import get_logger
from wax.state.audit_models import AuditEvent
from wax.state.authority_broker_models import (
    AuthorityGrantRecord,
    HumanHandoffRecord,
)
from wax.state.engine import db_session
from wax.state.workspace_models import WorkspaceSnapshotRecord

log = get_logger(__name__)

# Metrics names surfaced on the dashboard's runtime-activity card,
# in display order. Only counters that EXIST are rendered — a quiet
# runtime shows an honest "quiet" state, never fabricated numbers.
_DASHBOARD_COUNTERS = (
    ("bridge_messages_total", "Messages processed"),
    ("capability_invocations_total", "Capability invocations"),
    ("code_executions_total", "Code executions"),
    ("terminal_executions_total", "Terminal executions"),
    ("control_handoffs_submitted_total", "Handoffs submitted"),
    ("control_blob_gc_removed_total", "GC objects removed"),
    ("approvals_requested_total", "Approvals requested"),
    ("work_items_processed_total", "Work items processed"),
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_SESSION_COOKIE = "wax_control_session"
_SESSION_TTL_SECONDS = 12 * 3600
_CSRF_TTL_SECONDS = 15 * 60

# Login brute-force protection (in-memory sliding window). Deliberately
# conservative defaults: 8 failed attempts per IP per 5 minutes.
_LOGIN_MAX_FAILURES = 8
_LOGIN_WINDOW_SECONDS = 5 * 60

# Dashboard page size: handoff table pages at 50 rows so an operator's
# browser never renders an unbounded table; older history stays reachable
# through the pager and the CSV export.
_PAGE_SIZE = 50
# CSV export cap: metadata-only rows are small, but the export is still
# bounded so a runaway table cannot balloon a single response.
_CSV_MAX_ROWS = 5000
# Audit-ledger export cap (same reasoning; the ledger is append-only and
# grows without bound — the paginated page reaches older rows).
_AUDIT_CSV_MAX_ROWS = 5000

# Statuses shown as "actionable" on the dashboard and submittable.
OPEN_STATUSES = (
    HandoffStatus.PENDING.value,
    HandoffStatus.OPENED.value,
    HandoffStatus.AWAITING_HUMAN.value,
)
# Terminal-bad-outcome statuses grouped under the "failed" tab.
FAILED_STATUSES = (
    HandoffStatus.REJECTED.value,
    HandoffStatus.FAILED.value,
    HandoffStatus.EXPIRED.value,
    HandoffStatus.CANCELLED.value,
)

_STATUS_FILTERS = ("all", "open", "completed", "failed")

# Authority-grant status tabs (?status= on /control/grants). "expired" is
# computed live: a grant whose status is still active but whose expires_at
# has passed shows as expired everywhere (the stored status only changes
# when something touches the record).
_GRANT_STATUS_FILTERS = ("all", "active", "revoked", "expired")

# Date-range filters (?since=YYYY-MM-DD&until=YYYY-MM-DD, UTC days).
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ?q= operator search: literal substring over purpose / origin, or ID
# prefix. Capped — a needle this long is pasted noise, not a search.
_SEARCH_MAX_LEN = 100

# Audit-page tabs (?kind= on /control/audit). Each tab is a prefix over
# the event_kind namespace written by this module. "maintenance" matches
# BOTH the operator-triggered GC (control.blob_gc) and the system's
# scheduled GC (system.blob_gc, written by the maintenance pass).
_AUDIT_KIND_FILTERS = ("all", "login", "handoffs", "grants", "maintenance")
_AUDIT_KIND_PREFIX = {
    "login": "control.login",
    "handoffs": "control.handoff",
    "grants": "control.grant",
    "maintenance": "control.blob_gc",
}

# Growth warning threshold for the append-only audit ledger: when no
# retention policy is configured and the ledger passes this many rows,
# the audit page says so honestly instead of growing silently forever.
_AUDIT_WARN_ROWS = 50_000

_MATERIAL_LABELS = {
    MaterialType.OPAQUE_SECRET.value: "Opaque secret (API key, password, token)",
    MaterialType.SESSION_MATERIAL.value: "Session material (cookie blob, session id)",
    MaterialType.DELEGATED_GRANT.value: "Delegated grant (OAuth code, pre-auth link)",
    MaterialType.BROWSER_SESSION_REFERENCE.value: "Browser session reference",
}


# ---------------------------------------------------------------------------
# Auth + CSRF helpers
# ---------------------------------------------------------------------------


def _hmac_hex(key: str, payload: str) -> str:
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


class LoginRateLimiter:
    """In-memory sliding-window limiter for control-plane logins.

    Keyed by client IP. Only FAILED attempts count; a successful login
    clears the window for that IP. Process-local by design: a restart
    clears state, and multi-worker deployments should front this with a
    real WAF anyway (this limiter is defense-in-depth, not the perimeter).
    """

    def __init__(self, max_failures: int = _LOGIN_MAX_FAILURES) -> None:
        self._max = max_failures
        self._lock = threading.Lock()
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._failures.get(key)
            if not q:
                return False
            while q and now - q[0] > _LOGIN_WINDOW_SECONDS:
                q.popleft()
            return len(q) >= self._max

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._failures[key]
            q.append(now)
            while q and now - q[0] > _LOGIN_WINDOW_SECONDS:
                q.popleft()

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset(self) -> None:
        """Clear all state (test hygiene / operational reset)."""
        with self._lock:
            self._failures.clear()


_login_limiter = LoginRateLimiter()


def _client_ip(request: Request) -> str:
    client = request.client
    return client.host if client is not None else "unknown"


async def _audit_action(
    session,
    *,
    event_kind: str,
    outcome: str,
    payload: dict[str, Any] | None = None,
    request: Request | None = None,
    actor_kind: str = "human",
) -> None:
    """Append a control-plane row to the append-only audit_events ledger.

    INV-06: every security-sensitive action must be attributable. Operators
    authenticate with the shared deployment token, so there is no principal
    row — the client IP + auth surface travels in the payload instead. The
    write shares the caller's transaction, so the audit row exists if and
    only if the action commits. A failing audit write NEVER blocks the
    action it records (the gap is loud in logs, not silent in code).
    Payloads are metadata-only — never secrets, never ciphertext.
    """
    from wax.observability.audit import record_audit_event

    enriched = dict(payload or {})
    if request is not None:
        enriched.setdefault("client_ip", _client_ip(request))
    try:
        await record_audit_event(
            session,
            actor_principal_id=None,
            actor_kind=actor_kind,
            event_kind=event_kind,
            outcome=outcome,
            payload=enriched,
        )
    except Exception:
        log.warning("control.audit_write_failed", event_kind=event_kind)


class ControlPlaneAuth:
    """Token auth + signed CSRF tokens for the control plane."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings

    @property
    def token(self) -> str:
        return (getattr(self._settings, "control_plane_token", "") or "").strip()

    @property
    def enabled(self) -> bool:
        """True when an operator token is configured."""
        return bool(self.token)

    @property
    def dev_open(self) -> bool:
        """True when the dashboard may open without a token (dev only)."""
        return not self.enabled and not self._settings.is_production

    def _key(self) -> str:
        # HMAC key for session/CSRF signing: the master secret when set,
        # otherwise the operator token, otherwise a fixed dev string (dev
        # only — production enforces both via enforce_production_hardening
        # + the 503 guard below, loudly).
        return (
            self._settings.secret_key
            or self.token
            or "wax-dev-insecure-signing-key-do-not-use-in-production"
        )

    # -- sessions ---------------------------------------------------------

    def session_cookie_value(self) -> str:
        exp = int(time.time()) + _SESSION_TTL_SECONDS
        return f"{exp}.{_hmac_hex(self._key(), f'control-session:{exp}:{self.token}')}"

    def verify_session(self, value: str | None) -> bool:
        if not value or "." not in value:
            return False
        exp_s, sig = value.split(".", 1)
        if not exp_s.isdigit():
            return False
        if int(exp_s) < time.time():
            return False
        expected = _hmac_hex(self._key(), f"control-session:{exp_s}:{self.token}")
        return hmac.compare_digest(sig, expected)

    # -- CSRF --------------------------------------------------------------

    def make_csrf(self, scope: str = "handoff") -> str:
        import secrets as _secrets

        exp = int(time.time()) + _CSRF_TTL_SECONDS
        nonce_value = _secrets.token_hex(8)
        payload = f"control-csrf:{scope}:{exp}:{nonce_value}"
        return f"{scope}.{exp}.{nonce_value}.{_hmac_hex(self._key(), payload)}"

    def verify_csrf(self, token: str | None, scope: str = "handoff") -> bool:
        if not token or token.count(".") != 3:
            return False
        token_scope, exp_s, nonce, sig = token.split(".")
        if token_scope != scope:
            return False
        if not exp_s.isdigit():
            return False
        if int(exp_s) < time.time():
            return False
        payload = f"control-csrf:{scope}:{exp_s}:{nonce}"
        expected = _hmac_hex(self._key(), payload)
        return hmac.compare_digest(sig, expected)

    # -- request authentication ---------------------------------------------

    def is_authenticated(self, request: Request) -> bool:
        """Accept the token via bearer header, X-Control-Token, ?token=,
        or an already-established session cookie."""
        if self.dev_open:
            return True
        if not self.enabled:
            return False  # production without token — never authenticate

        provided = request.headers.get("X-Control-Token", "").strip()
        if not provided:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                provided = auth_header[7:].strip()
        if not provided:
            provided = (request.query_params.get("token") or "").strip()
        if provided and hmac.compare_digest(provided, self.token):
            return True
        return self.verify_session(request.cookies.get(_SESSION_COOKIE))


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _html_response(request: Request, name: str, context: dict[str, Any], status_code: int = 200):
    response = templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )
    _harden(response)
    return response


def _harden(response) -> None:
    """Same-origin hardening headers on every control-plane response."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"


def _redirect(target: str) -> RedirectResponse:
    response = RedirectResponse(target, status_code=303)
    _harden(response)
    return response


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_control_plane(app) -> None:
    """Attach the /control/* dashboard routes to the FastAPI app."""

    @app.get("/control", tags=["control"], include_in_schema=False)
    async def control_dashboard(request: Request):
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)

        if not auth.enabled and settings.is_production:
            return _html_response(
                request,
                "error.html",
                {
                    "error": (
                        "Control plane disabled: set WAX_CONTROL_PLANE_TOKEN "
                        "before enabling the dashboard in production."
                    )
                },
                status_code=503,
            )
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        status_filter = (request.query_params.get("status") or "all").strip()
        if status_filter not in _STATUS_FILTERS:
            status_filter = "all"
        gc_flash = (request.query_params.get("gc") or "").strip()
        gc_result = _parse_gc_flash(gc_flash)
        bulk_flash = _parse_bulk_flash((request.query_params.get("bulk") or "").strip())
        try:
            page = int(request.query_params.get("page") or "1")
        except ValueError:
            page = 1
        page = max(1, page)

        q_raw = _parse_search_param(request)

        try:
            since_dt, since_raw = _parse_date_param(request, "since")
            until_dt, until_raw = _parse_date_param(request, "until")
        except ValueError as e:
            return _html_response(request, "error.html", {"error": str(e)}, status_code=400)
        filter_qs = _filter_query_string(("since", since_raw), ("until", until_raw), ("q", q_raw))
        date_only_qs = _filter_query_string(("since", since_raw), ("until", until_raw))
        q_qs = _filter_query_string(("q", q_raw))

        async with db_session() as session:
            counts = await _handoff_counts(session)
            # Last GC fact for the storage card: the most recent blob-GC
            # audit event (scheduled system sweep OR operator-run). One
            # indexed query; the card says honestly when storage was
            # last cleaned and by whom.
            last_gc_row = (
                (
                    await session.execute(
                        select(AuditEvent)
                        .where(AuditEvent.event_kind.in_(("system.blob_gc", "control.blob_gc")))
                        .order_by(AuditEvent.created_at.desc())
                        .limit(1)
                    )
                )
                .scalars()
                .first()
            )
            where = _combine_where(
                _status_where(status_filter),
                _date_where(since_dt, until_dt),
                _search_where(q_raw),
            )

            filtered_count = counts["total"]
            if where is not None:
                filtered_count = (
                    await session.execute(
                        select(func.count()).select_from(HumanHandoffRecord).where(where)
                    )
                ).scalar_one()
            pages = max(1, math.ceil(filtered_count / _PAGE_SIZE))
            if page > pages:
                page = pages

            query = select(HumanHandoffRecord).order_by(
                func.coalesce(
                    HumanHandoffRecord.created_at_col, HumanHandoffRecord.created_at
                ).desc()
            )
            if where is not None:
                query = query.where(where)
            rows = (
                (await session.execute(query.offset((page - 1) * _PAGE_SIZE).limit(_PAGE_SIZE)))
                .scalars()
                .all()
            )
            await session.commit()

        handoffs = [_handoff_view(h) for h in rows]
        blob_stats = _blob_stats(app)
        return _html_response(
            request,
            "dashboard.html",
            {
                "open_count": counts["open"],
                "completed_count": counts["completed"],
                "failed_count": counts["failed"],
                "total_count": counts["total"],
                "active_grants": counts["grants"],
                "handoffs": handoffs,
                "status_filter": status_filter,
                "status_filters": _STATUS_FILTERS,
                "since_filter": since_raw,
                "until_filter": until_raw,
                "q_filter": q_raw,
                "filter_qs": filter_qs,
                "date_only_qs": date_only_qs,
                "q_qs": q_qs,
                "page": page,
                "pages": pages,
                "page_size": _PAGE_SIZE,
                "filtered_count": filtered_count,
                "showing_from": (page - 1) * _PAGE_SIZE + 1 if rows else 0,
                "showing_to": (page - 1) * _PAGE_SIZE + len(rows),
                "page_window": _page_window(page, pages),
                "env": settings.env.value,
                "isolation_backend": settings.isolation_backend,
                "dev_open": auth.dev_open,
                "blob_objects": blob_stats["objects"] if blob_stats else None,
                "blob_bytes": blob_stats["bytes"] if blob_stats else None,
                "blob_gc_enabled": bool(getattr(settings, "blob_gc_enabled", False)),
                "last_gc": _last_gc_view(last_gc_row),
                "gc_result": gc_result,
                "bulk_flash": bulk_flash,
                "bulk_csrf": auth.make_csrf("cancel"),
                "gc_csrf": auth.make_csrf("gc") if blob_stats is not None else "",
                "runtime_counters": _runtime_counters(app),
                "wax_version": _wax_version,
            },
        )

    @app.get("/control/handoffs/export.csv", tags=["control"], include_in_schema=False)
    async def control_handoff_export_csv(request: Request):
        """Metadata-only CSV export (same auth as the dashboard).

        Registered BEFORE the /control/handoffs/{handoff_id} detail route:
        FastAPI matches in registration order, so the literal path must
        win over the ID pattern or "export.csv" would be treated as an
        unknown handoff ID.

        Contains NO secret material — only statuses, timestamps and safe
        reasons. Cells that could be interpreted as spreadsheet formulas
        (= + - @ prefixes) are neutralized on export.
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        status_filter = (request.query_params.get("status") or "all").strip()
        if status_filter not in _STATUS_FILTERS:
            status_filter = "all"
        q_raw = _parse_search_param(request)
        try:
            since_dt, _since_raw = _parse_date_param(request, "since")
            until_dt, _until_raw = _parse_date_param(request, "until")
        except ValueError as e:
            return _html_response(request, "error.html", {"error": str(e)}, status_code=400)

        async with db_session() as session:
            query = (
                select(HumanHandoffRecord)
                .order_by(
                    func.coalesce(
                        HumanHandoffRecord.created_at_col, HumanHandoffRecord.created_at
                    ).desc()
                )
                .limit(_CSV_MAX_ROWS)
            )
            where = _combine_where(
                _status_where(status_filter),
                _date_where(since_dt, until_dt),
                _search_where(q_raw),
            )
            if where is not None:
                query = query.where(where)
            rows = (await session.execute(query)).scalars().all()
            await session.commit()

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        response = Response(
            content=_handoffs_csv([_handoff_view(h) for h in rows]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="wax-handoffs-{stamp}.csv"'},
        )
        _harden(response)
        return response

    @app.get("/control/grants", tags=["control"], include_in_schema=False)
    async def control_grants(request: Request):
        """Authority-grant audit view (same auth as the dashboard).

        Operators otherwise have NO way to see live delegations — the
        dashboard only shows a count. This page lists every grant with its
        scope, effect class and lifecycle state; "expired" is computed
        live from expires_at so stale grants are visible even though no
        process has touched the row.
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        grant_status = (request.query_params.get("status") or "all").strip()
        if grant_status not in _GRANT_STATUS_FILTERS:
            grant_status = "all"
        revoked_flash = (request.query_params.get("revoked") or "").strip()
        revoke_flash = (request.query_params.get("revoke") or "").strip()
        try:
            page = int(request.query_params.get("page") or "1")
        except ValueError:
            page = 1
        page = max(1, page)

        async with db_session() as session:
            now = datetime.now(UTC)
            base_count = select(func.count()).select_from(AuthorityGrantRecord)
            grant_counts = {
                "all": (await session.execute(base_count)).scalar_one(),
                "active": (
                    await session.execute(base_count.where(_grant_status_where("active", now)))
                ).scalar_one(),
                "revoked": (
                    await session.execute(base_count.where(_grant_status_where("revoked", now)))
                ).scalar_one(),
                "expired": (
                    await session.execute(base_count.where(_grant_status_where("expired", now)))
                ).scalar_one(),
            }

            where = _grant_status_where(grant_status, now)
            filtered_count = grant_counts["all"]
            if where is not None:
                filtered_count = (
                    await session.execute(
                        select(func.count()).select_from(AuthorityGrantRecord).where(where)
                    )
                ).scalar_one()
            pages = max(1, math.ceil(filtered_count / _PAGE_SIZE))
            if page > pages:
                page = pages

            query = select(AuthorityGrantRecord).order_by(AuthorityGrantRecord.created_at.desc())
            if where is not None:
                query = query.where(where)
            rows = (
                (await session.execute(query.offset((page - 1) * _PAGE_SIZE).limit(_PAGE_SIZE)))
                .scalars()
                .all()
            )
            await session.commit()

        return _html_response(
            request,
            "grants.html",
            {
                "grants": [_grant_view(g, now) for g in rows],
                "grant_status_filter": grant_status,
                "grant_status_filters": _GRANT_STATUS_FILTERS,
                "grant_counts": grant_counts,
                "filtered_count": filtered_count,
                "showing_from": (page - 1) * _PAGE_SIZE + 1 if rows else 0,
                "showing_to": (page - 1) * _PAGE_SIZE + len(rows),
                "page": page,
                "pages": pages,
                "page_size": _PAGE_SIZE,
                "page_window": _page_window(page, pages),
                "revoke_csrf": auth.make_csrf("revoke"),
                "revoked_flash": revoked_flash == "1",
                "revoke_flash": revoke_flash,
                "heartbeat": True,
                "dev_open": auth.dev_open,
                "wax_version": _wax_version,
            },
        )

    @app.get("/control/grants/export.csv", tags=["control"], include_in_schema=False)
    async def control_grants_export_csv(request: Request):
        """Metadata-only CSV export of authority grants (same auth).

        Honors the grants status tabs. NEVER contains the handle — the
        handle IS the live authority token and must not leave the system
        through an operator export (pinned by test). Status is the
        live-computed display state; the action COUNT is exported, never
        the action text. Formula prefixes neutralized like every export.
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        grant_status = (request.query_params.get("status") or "all").strip()
        if grant_status not in _GRANT_STATUS_FILTERS:
            grant_status = "all"

        async with db_session() as session:
            now = datetime.now(UTC)
            where = _grant_status_where(grant_status, now)
            query = (
                select(AuthorityGrantRecord)
                .order_by(AuthorityGrantRecord.created_at.desc())
                .limit(_CSV_MAX_ROWS)
            )
            if where is not None:
                query = query.where(where)
            rows = (await session.execute(query)).scalars().all()
            await session.commit()

        counts = [
            len(g.allowed_actions_json.get("actions") or [])
            if isinstance(g.allowed_actions_json, dict)
            else 0
            for g in rows
        ]
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        response = Response(
            content=_grants_csv([_grant_view(g, now) for g in rows], counts),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="wax-grants-{stamp}.csv"'},
        )
        _harden(response)
        return response

    @app.post("/control/grants/{grant_id}/revoke", tags=["control"], include_in_schema=False)
    async def control_grant_revoke(request: Request, grant_id: str):
        """Operator revokes a live authority grant.

        Broker-side transition (grant + underlying material both marked
        revoked) behind a NEW scoped CSRF ("revoke") — tokens minted for
        handoff submit/cancel/GC forms are rejected. The revoke button is
        confirm()-guarded in the UI; the server treats revocation as
        immediate and irreversible (a new delegation requires a new
        handoff). Keyed by the grant's RECORD id — the handle (the live
        authority token) never appears in operator HTML or URLs.
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        form = await request.form()
        if not auth.verify_csrf(str(form.get("csrf_token", "") or ""), scope="revoke"):
            log.warning("control.grant_revoke_csrf_rejected")
            return _redirect("/control/grants?revoke=stale_token")

        async with db_session() as session:
            from wax.runtime.authority.broker import AuthorityBroker

            services = getattr(app.state, "services", None)
            broker = (
                services.authority_broker
                if services is not None and services.authority_broker is not None
                else AuthorityBroker()
            )
            revoked = await broker.operator_revoke_authority(session, grant_id=grant_id)
            if revoked:
                await _audit_action(
                    session,
                    event_kind="control.grant_revoked",
                    outcome="success",
                    payload={"grant_id": grant_id},
                    request=request,
                )
            else:
                await _audit_action(
                    session,
                    event_kind="control.grant_revoked",
                    outcome="failed",
                    payload={"grant_id": grant_id, "reason": "unknown_grant"},
                    request=request,
                )
            await session.commit()

        if not revoked:
            log.warning("control.grant_revoke_unknown_handle")
            return _redirect("/control/grants?revoke=unknown")
        log.info("control.grant_revoked")
        return _redirect("/control/grants?revoked=1")

    @app.get("/control/audit", tags=["control"], include_in_schema=False)
    async def control_audit(request: Request):
        """Operator audit trail (same auth as the dashboard).

        Renders the append-only audit_events rows this module writes on
        every security-sensitive action (INV-06): sign-ins/out, handoff
        submits/cancels, grant revocations, blob GC runs — plus the
        system-written scheduled-GC events (system.blob_gc, deletions
        only). Filterable by kind tab AND a metadata-only ?q= search
        (event names + payload IDs — paste a handoff/grant ID from a
        detail page to see exactly that entity's events). Metadata-only —
        payloads never contain secrets or ciphertext.
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        kind_filter = (request.query_params.get("kind") or "all").strip()
        if kind_filter not in _AUDIT_KIND_FILTERS:
            kind_filter = "all"
        q_filter = _parse_search_param(request)
        try:
            page = int(request.query_params.get("page") or "1")
        except ValueError:
            page = 1
        page = max(1, page)
        retention_days = int(getattr(settings, "audit_retention_days", 0) or 0)

        async with db_session() as session:
            base_where = _audit_base_where()
            total = (
                await session.execute(
                    select(func.count()).select_from(AuditEvent).where(base_where)
                )
            ).scalar_one()
            kind_counts = await _audit_family_counts(session)
            where = _combine_where(
                _audit_kind_where(kind_filter),
                _audit_search_where(q_filter),
            )
            filtered_count = total
            if where is not None:
                filtered_count = (
                    await session.execute(select(func.count()).select_from(AuditEvent).where(where))
                ).scalar_one()
            pages = max(1, math.ceil(filtered_count / _PAGE_SIZE))
            if page > pages:
                page = pages

            query = select(AuditEvent).where(base_where).order_by(AuditEvent.created_at.desc())
            if where is not None:
                query = query.where(where)
            rows = (
                (await session.execute(query.offset((page - 1) * _PAGE_SIZE).limit(_PAGE_SIZE)))
                .scalars()
                .all()
            )
            await session.commit()

        return _html_response(
            request,
            "audit.html",
            {
                "events": [_audit_view(e) for e in rows],
                "kind_filter": kind_filter,
                "kind_filters": _AUDIT_KIND_FILTERS,
                "kind_counts": kind_counts,
                "retention_days": retention_days,
                "retention_warning": _retention_warning(
                    total, retention_days, threshold=_AUDIT_WARN_ROWS
                ),
                "heartbeat": True,
                "dev_open": auth.dev_open,
                "q_filter": q_filter,
                "filter_qs": _filter_query_string(("kind", kind_filter), ("q", q_filter)),
                "total_count": total,
                "filtered_count": filtered_count,
                "showing_from": (page - 1) * _PAGE_SIZE + 1 if rows else 0,
                "showing_to": (page - 1) * _PAGE_SIZE + len(rows),
                "page": page,
                "pages": pages,
                "page_size": _PAGE_SIZE,
                "page_window": _page_window(page, pages),
                "wax_version": _wax_version,
            },
        )

    @app.get("/control/audit/export.csv", tags=["control"], include_in_schema=False)
    async def control_audit_export_csv(request: Request):
        """Metadata-only CSV export of the audit ledger (same auth).

        Honors the same ?kind= / ?q= filters as the page. Payloads are
        exported as structured safe columns plus the raw payload JSON
        (metadata-only by construction — payloads never contain secrets
        or ciphertext, pinned by tests). Spreadsheet formula prefixes are
        neutralized exactly like the handoffs export. Capped: the ledger
        is append-only and could otherwise balloon a single response —
        older history stays reachable through the paginated page.
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        kind_filter = (request.query_params.get("kind") or "all").strip()
        if kind_filter not in _AUDIT_KIND_FILTERS:
            kind_filter = "all"
        q_filter = _parse_search_param(request)

        async with db_session() as session:
            where = _combine_where(
                _audit_base_where(),
                _audit_kind_where(kind_filter),
                _audit_search_where(q_filter),
            )
            query = (
                select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(_AUDIT_CSV_MAX_ROWS)
            )
            if where is not None:
                query = query.where(where)
            rows = (await session.execute(query)).scalars().all()
            await session.commit()

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        response = Response(
            content=_audit_csv(rows),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="wax-audit-{stamp}.csv"'},
        )
        _harden(response)
        return response

    @app.post("/control/handoffs/cancel-bulk", tags=["control"], include_in_schema=False)
    async def control_handoff_cancel_bulk(request: Request):
        """Operator cancels several OPEN handoffs in one go (noise cleanup).

        Reuses the "cancel" CSRF scope (same action, same authority) and
        the same broker transition as the single cancel — one audit event
        per cancelled handoff, one summary flash. Capped at one page of
        rows (50): bulk housekeeping, not bulk deletion.
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        form = await request.form()
        if not auth.verify_csrf(str(form.get("csrf_token", "") or ""), scope="cancel"):
            log.warning("control.cancel_bulk_csrf_rejected")
            return _redirect("/control?bulk=stale_token")

        ids = [v.strip() for v in form.getlist("handoff_ids") if isinstance(v, str)]
        ids = [i for i in ids if i][:_PAGE_SIZE]
        if not ids:
            return _redirect("/control?bulk=none")

        from wax.runtime.authority.broker import AuthorityBroker

        services = getattr(app.state, "services", None)
        broker = (
            services.authority_broker
            if services is not None and services.authority_broker is not None
            else AuthorityBroker()
        )

        cancelled: list[str] = []
        failed = 0
        async with db_session() as session:
            for hid in ids:
                handoff = await session.get(HumanHandoffRecord, hid)
                if handoff is None:
                    failed += 1
                    continue
                try:
                    await broker.cancel_handoff(
                        session,
                        handoff_id=handoff.id,
                        principal_id=handoff.principal_id,
                        reason_safe="Cancelled in bulk via the control plane.",
                    )
                except ValueError:
                    # raised BEFORE any mutation (unknown/wrong status) —
                    # nothing to roll back for this row
                    failed += 1
                    continue
                await _audit_action(
                    session,
                    event_kind="control.handoff_cancelled",
                    outcome="success",
                    payload={"handoff_id": handoff.id, "reason": "bulk_cancel"},
                    request=request,
                )
                cancelled.append(handoff.id)
            await session.commit()

        log.info("control.handoff_cancelled_bulk", cancelled=len(cancelled), failed=failed)
        return _redirect(f"/control?bulk=cancelled:{len(cancelled)},failed:{failed}")

    @app.get("/control/login", tags=["control"], include_in_schema=False)
    async def control_login_form(request: Request):
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if settings.is_production and not auth.enabled:
            return _html_response(
                request,
                "error.html",
                {
                    "error": (
                        "Control plane disabled: set WAX_CONTROL_PLANE_TOKEN "
                        "before enabling the dashboard in production."
                    )
                },
                status_code=503,
            )
        return _html_response(
            request,
            "login.html",
            {"dev_open": auth.dev_open, "wax_version": _wax_version},
        )

    @app.post("/control/login", tags=["control"], include_in_schema=False)
    async def control_login(request: Request):
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if settings.is_production and not auth.enabled:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)

        ip = _client_ip(request)
        if _login_limiter.blocked(ip):
            log.warning("control.login_rate_limited", client_ip=ip)
            async with db_session() as session:
                await _audit_action(
                    session,
                    event_kind="control.login",
                    outcome="denied",
                    payload={"reason": "rate_limited"},
                    request=request,
                )
                await session.commit()
            return _html_response(
                request,
                "login.html",
                {
                    "dev_open": auth.dev_open,
                    "error": (
                        "Too many failed attempts. Wait a few minutes and try again — "
                        "further attempts are temporarily refused."
                    ),
                },
                status_code=429,
            )

        form = await request.form()
        provided = str(form.get("token", "") or "").strip()
        if not auth.enabled:
            if provided:
                return _html_response(
                    request,
                    "login.html",
                    {
                        "dev_open": True,
                        "error": "No token is configured; the dashboard is open in development.",
                    },
                )
            return _redirect("/control")
        if not provided or not hmac.compare_digest(provided, auth.token):
            _login_limiter.record_failure(ip)
            log.warning("control.login_failed", client_ip=ip)
            async with db_session() as session:
                await _audit_action(
                    session,
                    event_kind="control.login",
                    outcome="denied",
                    payload={"reason": "invalid_token"},
                    request=request,
                )
                await session.commit()
            return _html_response(
                request, "login.html", {"dev_open": False, "error": "Invalid access token."}, 401
            )
        _login_limiter.clear(ip)
        response = _redirect("/control")
        response.set_cookie(
            _SESSION_COOKIE,
            auth.session_cookie_value(),
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=settings.is_production,
        )
        async with db_session() as session:
            await _audit_action(
                session,
                event_kind="control.login",
                outcome="success",
                payload={"via": "token"},
                request=request,
            )
            await session.commit()
        return response

    @app.post("/control/logout", tags=["control"], include_in_schema=False)
    async def control_logout(request: Request):
        async with db_session() as session:
            await _audit_action(
                session,
                event_kind="control.logout",
                outcome="success",
                request=request,
            )
            await session.commit()
        response = _redirect("/control/login")
        response.delete_cookie(_SESSION_COOKIE)
        return response

    @app.get("/control/handoffs/{handoff_id}", tags=["control"], include_in_schema=False)
    async def control_handoff_detail(request: Request, handoff_id: str):
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        async with db_session() as session:
            handoff = await session.get(HumanHandoffRecord, handoff_id)
            if handoff is None:
                return _html_response(
                    request, "error.html", {"error": "No such handoff."}, status_code=404
                )
            # Honest progress: viewing the handoff marks it opened.
            if handoff.status == HandoffStatus.PENDING.value:
                handoff.status = HandoffStatus.OPENED.value
                handoff.opened_at = datetime.now(UTC)
            await session.commit()
            view = _handoff_view(handoff)
            # An expired challenge is NOT submittable: the form is replaced
            # by an explicit expired panel so the operator never types a
            # secret the runtime would have to refuse.
            submittable = view["open"] and not view["expired"]
            csrf = auth.make_csrf() if submittable else ""
            # Cancelling stays available for ANY open handoff (including an
            # expired-but-not-yet-marked one): it is the cleanup affordance
            # for stale requests, so it intentionally outlives the challenge.
            cancel_csrf = auth.make_csrf("cancel") if view["open"] else ""

        cancel_flash: dict[str, str] | None = None
        cancel_param = (request.query_params.get("cancel") or "").strip()
        if request.query_params.get("cancelled") == "1":
            cancel_flash = {
                "kind": "ok",
                "message": (
                    "Handoff cancelled — the waiting intelligence was notified and will "
                    "observe the cancellation instead of blocking on this request."
                ),
            }
        elif cancel_param == "stale_token":
            cancel_flash = {
                "kind": "error",
                "message": "The cancel request was rejected (stale token) — please retry.",
            }
        elif cancel_param == "error":
            cancel_flash = {
                "kind": "error",
                "message": "The handoff can no longer be cancelled (it already reached a terminal state).",
            }

        return _html_response(
            request,
            "handoff_detail.html",
            {
                "h": view,
                "csrf": csrf,
                "cancel_csrf": cancel_csrf,
                "cancel_flash": cancel_flash,
                "dev_open": auth.dev_open,
                "material_labels": _material_choices(view["kind"]),
                # Metadata-only JSON view of this handoff (the same fields
                # the page already renders — no secret material, no
                # ciphertext, no challenge hash) for operator debugging.
                "metadata_json": json.dumps(view, indent=2, sort_keys=True),
                "wax_version": _wax_version,
            },
        )

    @app.post("/control/handoffs/{handoff_id}/submit", tags=["control"], include_in_schema=False)
    async def control_handoff_submit(request: Request, handoff_id: str):
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        form = await request.form()
        secret_value = str(form.get("secret_value", "") or "")
        material_type_form = str(form.get("material_type", "") or "").strip()
        csrf_token = str(form.get("csrf_token", "") or "")

        async with db_session() as session:
            handoff = await session.get(HumanHandoffRecord, handoff_id)
            if handoff is None:
                return _html_response(
                    request, "error.html", {"error": "No such handoff."}, status_code=404
                )

            if not auth.verify_csrf(csrf_token):
                log.warning("control.submit_csrf_rejected", handoff_id=handoff_id)
                await _audit_action(
                    session,
                    event_kind="control.handoff_submitted",
                    outcome="denied",
                    payload={"handoff_id": handoff_id, "reason": "stale_csrf"},
                    request=request,
                )
                await session.commit()
                view = _handoff_view(handoff)
                return _html_response(
                    request,
                    "handoff_detail.html",
                    {
                        "h": view,
                        "csrf": auth.make_csrf(),
                        "material_labels": _material_choices(view["kind"]),
                        "wax_version": _wax_version,
                        "dev_open": auth.dev_open,
                        "error": "Session expired — the form token is stale. Please submit again.",
                    },
                    status_code=403,
                )

            if not secret_value.strip():
                view = _handoff_view(handoff)
                return _html_response(
                    request,
                    "handoff_detail.html",
                    {
                        "h": view,
                        "csrf": auth.make_csrf(),
                        "material_labels": _material_choices(view["kind"]),
                        "wax_version": _wax_version,
                        "dev_open": auth.dev_open,
                        "error": "The value must not be empty.",
                    },
                    status_code=400,
                )

            # The material type follows the handoff kind unless the operator
            # explicitly chose an allowed alternative for a secret handoff.
            default_type = (
                MaterialType.BROWSER_SESSION_REFERENCE.value
                if view_kind(handoff) == "browser"
                else MaterialType.OPAQUE_SECRET.value
            )
            material_type = material_type_form or default_type

            from wax.runtime.authority.broker import AuthorityBroker

            services = getattr(app.state, "services", None)
            broker = (
                services.authority_broker
                if services is not None and services.authority_broker is not None
                else AuthorityBroker()
            )
            try:
                await broker.submit_handoff(
                    session,
                    handoff_id=handoff.id,
                    principal_id=handoff.principal_id,
                    secret_value=secret_value,
                    material_type=material_type,
                )
                await session.commit()
            except ValueError as e:
                await session.rollback()
                log.warning("control.submit_rejected", handoff_id=handoff_id, reason=str(e)[:200])
                async with db_session() as audit_session:
                    await _audit_action(
                        audit_session,
                        event_kind="control.handoff_submitted",
                        outcome="failed",
                        payload={"handoff_id": handoff_id, "reason": str(e)[:200]},
                        request=request,
                    )
                    await audit_session.commit()
                fresh = await session.get(HumanHandoffRecord, handoff_id)
                view = _handoff_view(fresh) if fresh is not None else _handoff_view(handoff)
                return _html_response(
                    request,
                    "handoff_detail.html",
                    {
                        "h": view,
                        "csrf": auth.make_csrf(),
                        "material_labels": _material_choices(view["kind"]),
                        "wax_version": _wax_version,
                        "dev_open": auth.dev_open,
                        "error": str(e),
                    },
                    status_code=400,
                )

        # SUCCESS: the secret is already encrypted. From here on only
        # opaque handles exist — the submitted value is NEVER echoed.
        log.info("control.handoff_submitted", handoff_id=handoff_id)
        async with db_session() as audit_session:
            await _audit_action(
                audit_session,
                event_kind="control.handoff_submitted",
                outcome="success",
                payload={"handoff_id": handoff_id, "material_type": material_type},
                request=request,
            )
            await audit_session.commit()
        metrics = getattr(getattr(app.state, "services", None), "metrics", None)
        if metrics is not None:
            metrics.control_handoff_submitted()
        return _redirect(f"/control/handoffs/{handoff_id}?submitted=1")

    @app.post("/control/handoffs/{handoff_id}/cancel", tags=["control"], include_in_schema=False)
    async def control_handoff_cancel(request: Request, handoff_id: str):
        """Operator cancels an OPEN handoff (noise-cleanup affordance).

        The broker marks the handoff `cancelled`, records a SAFE reason,
        and emits `authority.handoff_cancelled:{id}` so waiting work wakes
        immediately instead of blocking until the challenge expires.
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        form = await request.form()
        if not auth.verify_csrf(str(form.get("csrf_token", "") or ""), scope="cancel"):
            log.warning("control.cancel_csrf_rejected", handoff_id=handoff_id)
            return _redirect(f"/control/handoffs/{handoff_id}?cancel=stale_token")
        # The optional reason is operator-supplied metadata (never a secret
        # entry field) and is stored truncated + plainly labeled.
        reason = str(form.get("reason", "") or "").strip() or None

        async with db_session() as session:
            handoff = await session.get(HumanHandoffRecord, handoff_id)
            if handoff is None:
                return _html_response(
                    request, "error.html", {"error": "No such handoff."}, status_code=404
                )
            from wax.runtime.authority.broker import AuthorityBroker

            services = getattr(app.state, "services", None)
            broker = (
                services.authority_broker
                if services is not None and services.authority_broker is not None
                else AuthorityBroker()
            )
            try:
                await broker.cancel_handoff(
                    session,
                    handoff_id=handoff.id,
                    principal_id=handoff.principal_id,
                    reason_safe=reason,
                )
                await _audit_action(
                    session,
                    event_kind="control.handoff_cancelled",
                    outcome="success",
                    payload={"handoff_id": handoff.id, "reason": (reason or "")[:200]},
                    request=request,
                )
                await session.commit()
            except ValueError:
                await session.rollback()
                log.warning("control.cancel_rejected", handoff_id=handoff_id)
                async with db_session() as audit_session:
                    await _audit_action(
                        audit_session,
                        event_kind="control.handoff_cancelled",
                        outcome="failed",
                        payload={"handoff_id": handoff_id, "reason": "not_cancellable"},
                        request=request,
                    )
                    await audit_session.commit()
                return _redirect(f"/control/handoffs/{handoff_id}?cancel=error")

        log.info("control.handoff_cancelled", handoff_id=handoff_id)
        return _redirect(f"/control/handoffs/{handoff_id}?cancelled=1")

    @app.get("/control/api/status", tags=["control"])
    async def control_api_status(request: Request):
        """Read-only JSON status endpoint for external monitoring.

        Same authentication as the dashboard (session cookie or token).
        Deliberately metadata-only: counters, versions and storage size —
        never handoff contents, purposes, or any secret material.
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return JSONResponse({"error": "control plane disabled"}, status_code=503)
        if not auth.is_authenticated(request):
            return JSONResponse({"error": "not authenticated"}, status_code=401)

        async with db_session() as session:
            counts = await _handoff_counts(session)
        blob_stats = _blob_stats(app)
        payload: dict[str, Any] = {
            "status": "ok",
            "version": _wax_version,
            "env": settings.env.value,
            "isolation_backend": settings.isolation_backend,
            "utc_now": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "counts": {
                "handoffs_open": counts["open"],
                "handoffs_completed": counts["completed"],
                "handoffs_failed": counts["failed"],
                "handoffs_total": counts["total"],
                "authority_grants_active": counts["grants"],
            },
            "blob_gc_enabled": bool(getattr(settings, "blob_gc_enabled", False)),
        }
        if blob_stats is not None:
            payload["blob_store"] = {
                "objects": blob_stats["objects"],
                "bytes": blob_stats["bytes"],
            }
        response = JSONResponse(payload)
        _harden(response)
        return response

    @app.post("/control/maintenance/gc", tags=["control"], include_in_schema=False)
    async def control_maintenance_gc(request: Request):
        """Operator-triggered blob-store garbage collection.

        Deletes only blobs provably unreachable from any live workspace
        snapshot. Session-authenticated + CSRF-protected; the result is
        reported via a redirect flash (no secrets involved).
        """
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if not auth.enabled and settings.is_production:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)
        if not auth.is_authenticated(request):
            return _redirect("/control/login")

        form = await request.form()
        if not auth.verify_csrf(str(form.get("csrf_token", "") or ""), scope="gc"):
            log.warning("control.gc_csrf_rejected")
            return _redirect("/control?gc=error")

        blob_store = getattr(getattr(app.state, "services", None), "blob_store", None)
        if blob_store is None:
            return _redirect("/control?gc=unavailable")

        async with db_session() as session:
            result = await session.execute(select(WorkspaceSnapshotRecord.files_json))
            await session.commit()
        live: set[str] = set()
        for files in result.scalars():
            if isinstance(files, list):
                for entry in files:
                    if isinstance(entry, dict):
                        digest = entry.get("sha256")
                        if isinstance(digest, str) and digest:
                            live.add(digest)

        gc = blob_store.collect_garbage(live)
        metrics = getattr(getattr(app.state, "services", None), "metrics", None)
        if metrics is not None:
            metrics.control_blob_gc_run(
                removed=gc["removed"], reclaimed_bytes=gc["reclaimed_bytes"]
            )
        log.info("control.gc_run", removed=gc["removed"], reclaimed_bytes=gc["reclaimed_bytes"])
        async with db_session() as session:
            await _audit_action(
                session,
                event_kind="control.blob_gc",
                outcome="success",
                payload={
                    "removed": gc["removed"],
                    "reclaimed_bytes": gc["reclaimed_bytes"],
                },
                request=request,
            )
            await session.commit()
        return _redirect(f"/control?gc={gc['removed']}:{gc['reclaimed_bytes']}")


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------


async def _handoff_counts(session) -> dict[str, int]:
    """Aggregate counts shared by the dashboard and /control/api/status."""
    open_count = (
        await session.execute(
            select(func.count())
            .select_from(HumanHandoffRecord)
            .where(HumanHandoffRecord.status.in_(OPEN_STATUSES))
        )
    ).scalar_one()
    completed_count = (
        await session.execute(
            select(func.count())
            .select_from(HumanHandoffRecord)
            .where(HumanHandoffRecord.status == HandoffStatus.COMPLETED.value)
        )
    ).scalar_one()
    failed_count = (
        await session.execute(
            select(func.count())
            .select_from(HumanHandoffRecord)
            .where(HumanHandoffRecord.status.in_(FAILED_STATUSES))
        )
    ).scalar_one()
    total_count = (
        await session.execute(select(func.count()).select_from(HumanHandoffRecord))
    ).scalar_one()
    active_grants = (
        await session.execute(
            select(func.count())
            .select_from(AuthorityGrantRecord)
            .where(AuthorityGrantRecord.status == AuthorityStatus.ACTIVE.value)
        )
    ).scalar_one()
    return {
        "open": open_count,
        "completed": completed_count,
        "failed": failed_count,
        "total": total_count,
        "grants": active_grants,
    }


def _status_where(status_filter: str):
    """SQLAlchemy WHERE clause for a dashboard status filter (None = all)."""
    if status_filter == "open":
        return HumanHandoffRecord.status.in_(OPEN_STATUSES)
    if status_filter == "completed":
        return HumanHandoffRecord.status == HandoffStatus.COMPLETED.value
    if status_filter == "failed":
        return HumanHandoffRecord.status.in_(FAILED_STATUSES)
    return None


def _parse_date_param(request: Request, name: str) -> tuple[datetime | None, str]:
    """Parse a ?since=/&until= UTC-day filter (YYYY-MM-DD).

    Returns (day_start_utc, raw_string). Raises ValueError for malformed
    or impossible dates — misconfiguration must be loud (400), never a
    silently-ignored filter that makes the operator trust wrong data.
    """
    raw = (request.query_params.get(name) or "").strip()
    if not raw:
        return None, ""
    if not _DATE_RE.match(raw):
        raise ValueError(f"Invalid {name} filter: expected YYYY-MM-DD, got {raw!r}.")
    try:
        day = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as e:
        raise ValueError(f"Invalid {name} filter: {raw!r} is not a real calendar date.") from e
    return day, raw


def _date_where(since: datetime | None, until: datetime | None):
    """WHERE clause bounding the handoff creation day (coalescing the
    explicit created_at_col with the mixin timestamp), or None."""
    clauses = []
    created = func.coalesce(HumanHandoffRecord.created_at_col, HumanHandoffRecord.created_at)
    if since is not None:
        clauses.append(created >= since)
    if until is not None:
        clauses.append(created < until + timedelta(days=1))  # inclusive day
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    from sqlalchemy import and_

    return and_(*clauses)


def _filter_query_string(*pairs: tuple[str, str]) -> str:
    """urlencode'd query fragment skipping empty values — carries
    since/until/q across tabs, pager and CSV links so every filter
    survives navigation."""
    return urlencode([(k, v) for k, v in pairs if v])


def _parse_search_param(request: Request) -> str:
    """The ?q= operator search needle, trimmed and length-capped.

    Matching is literal substring (see _search_where) — % and _ are
    escaped, never treated as wildcards, so an operator searching for
    "100%_done" gets exactly that string.
    """
    raw = (request.query_params.get("q") or "").strip()
    if len(raw) > _SEARCH_MAX_LEN:
        raw = raw[:_SEARCH_MAX_LEN]
    return raw


def _escape_like(q: str) -> str:
    """Escape LIKE metacharacters so operator text is taken literally."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_where(q: str):
    """WHERE clause for the ?q= search, or None.

    Matches purpose / origin_reference as literal substrings and the
    handoff ID as a prefix (operators paste IDs). LIKE metacharacters are
    escaped so the operator's text is always taken literally.
    """
    if not q:
        return None
    escaped = _escape_like(q)
    from sqlalchemy import or_

    return or_(
        HumanHandoffRecord.purpose.ilike(f"%{escaped}%", escape="\\"),
        HumanHandoffRecord.origin_reference.ilike(f"%{escaped}%", escape="\\"),
        HumanHandoffRecord.id.ilike(f"{escaped}%", escape="\\"),
    )


def _audit_base_where():
    """The audit page shows everything THIS control plane wrote (control.*)
    plus system-written maintenance events (system.*)."""
    from sqlalchemy import or_

    return or_(
        AuditEvent.event_kind.like("control.%"),
        AuditEvent.event_kind.like("system.%"),
    )


def _audit_kind_where(kind_filter: str):
    """WHERE clause for an audit kind tab, or None ("all"). The
    maintenance tab covers both the operator-triggered GC and the
    system's scheduled GC; the login tab covers the whole auth-session
    family (sign-ins AND sign-outs — a sign-out has no other home)."""
    prefix = _AUDIT_KIND_PREFIX.get(kind_filter)
    if not prefix:
        return None
    from sqlalchemy import or_

    if kind_filter == "maintenance":
        return or_(
            AuditEvent.event_kind.like(f"{prefix}%"),
            AuditEvent.event_kind.like("system.%"),
        )
    if kind_filter == "login":
        return or_(
            AuditEvent.event_kind.like("control.login%"),
            AuditEvent.event_kind.like("control.logout%"),
        )
    return AuditEvent.event_kind.like(f"{prefix}%")


def _audit_search_where(q: str):
    """WHERE clause for the audit ?q= search, or None.

    Literal substring over event_kind and the serialized payload. Payloads
    are metadata-only by construction, and they carry the IDs an operator
    copies off a detail page (handoff_id, grant_id) — so pasting an ID
    finds exactly that entity's events. LIKE metacharacters escaped.
    """
    if not q:
        return None
    escaped = _escape_like(q)
    from sqlalchemy import or_

    return or_(
        AuditEvent.event_kind.ilike(f"%{escaped}%", escape="\\"),
        AuditEvent.payload.cast(String).ilike(f"%{escaped}%", escape="\\"),
    )


def _combine_where(*clauses):
    """AND-combine filter clauses, ignoring the Nones (each filter is
    optional), or None when no filter applies at all."""
    present = [c for c in clauses if c is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    from sqlalchemy import and_

    return and_(*present)


async def _audit_family_counts(session) -> dict[str, int]:
    """Per-tab event counts for the audit kind tabs, from ONE grouped
    query (no extra round-trip per tab). Family mapping mirrors
    _audit_kind_where — the maintenance tab covers both the
    operator-triggered GC (control.blob_gc) and the system's scheduled
    GC (system.*); the login tab covers sign-ins AND sign-outs. Kinds
    outside every family still count toward "all" (honest totals), they
    just have no dedicated tab."""
    rows = await session.execute(
        select(AuditEvent.event_kind, func.count())
        .where(_audit_base_where())
        .group_by(AuditEvent.event_kind)
    )
    counts = {f: 0 for f in _AUDIT_KIND_FILTERS}
    for kind, n in rows.all():
        n = int(n)
        counts["all"] += n
        if kind.startswith("control.login") or kind.startswith("control.logout"):
            counts["login"] += n
        elif kind.startswith("control.handoff"):
            counts["handoffs"] += n
        elif kind.startswith("control.grant"):
            counts["grants"] += n
        elif kind.startswith("control.blob_gc") or kind.startswith("system."):
            counts["maintenance"] += n
    return counts


def _retention_warning(
    total: int, retention_days: int, threshold: int = _AUDIT_WARN_ROWS
) -> str | None:
    """Growth warning for the audit page header, or None.

    The ledger is append-only (INV-06): with no retention policy it grows
    forever. Once it passes the threshold, the page says so plainly and
    names the setting that bounds it — a misconfigured deployment must
    not become a silent storage leak. A configured policy (> 0 days)
    means the ledger is bounded and never warns."""
    if retention_days > 0 or total < threshold:
        return None
    return (
        f"The audit ledger holds {total:,} events and no retention policy is "
        "configured — it grows unbounded. Set WAX_AUDIT_RETENTION_DAYS "
        "(days) to bound its growth."
    )


def _last_gc_view(row) -> dict[str, Any] | None:
    """Display projection of the most recent GC audit event for the
    dashboard's storage card — scheduled system sweep OR operator-run.
    Metadata only; the numbers come from the audit payload."""
    if row is None:
        return None
    payload = row.payload if isinstance(row.payload, dict) else {}
    try:
        removed = int(payload.get("removed", 0))
        reclaimed = int(payload.get("reclaimed_bytes", 0))
    except (TypeError, ValueError):
        removed, reclaimed = 0, 0
    when = row.created_at
    if when is not None and when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    scheduled = row.event_kind == "system.blob_gc"
    actor_label = "scheduled" if scheduled else "operator"
    return {
        "removed": removed,
        "reclaimed_kb": reclaimed / 1024,
        "when_rel": _rel_dt(when) or "—",
        "when_abs": when.strftime("%Y-%m-%d %H:%M:%SZ") if when else "",
        "scheduled": scheduled,
        "actor_label": actor_label,
        "title_text": f"last GC: {when.strftime('%Y-%m-%d %H:%M:%SZ') if when else ''} "
        f"({'scheduled sweep' if scheduled else 'operator run'})",
    }


def _grant_status_where(status_filter: str, now: datetime):
    """WHERE clause for the grants-page status tabs, or None.

    "active" means status is active AND not yet expired; "expired" is the
    live computation (status still active, expires_at in the past) — a
    grant row is only marked revoked by an explicit revocation.
    """
    from sqlalchemy import and_

    active = AuthorityGrantRecord.status == "active"
    if status_filter == "active":
        return and_(active, AuthorityGrantRecord.expires_at >= now)
    if status_filter == "revoked":
        return AuthorityGrantRecord.status == "revoked"
    if status_filter == "expired":
        return and_(active, AuthorityGrantRecord.expires_at < now)
    return None


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite/aiosqlite return naive datetimes even for timezone=True
    columns — normalize to UTC before any comparison or arithmetic."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _grant_view(g: AuthorityGrantRecord, now: datetime) -> dict[str, Any]:
    """Display projection of a grant — identifiers are shortened (full
    values in title attributes); no secret material exists on the record."""
    created = _aware(g.created_at)
    expires = _aware(g.expires_at)
    revoked = _aware(g.revoked_at)
    last_used = _aware(g.last_used_at)
    status = g.status
    if status == "active" and expires is not None and expires < now:
        status = "expired"
    actions: list[str] = []
    if isinstance(g.allowed_actions_json, dict):
        raw_actions = g.allowed_actions_json.get("actions") or []
        actions = [str(a)[:80] for a in raw_actions][:6]
    expires_soon = (
        status == "active" and expires is not None and (expires - now).total_seconds() < 6 * 3600
    )
    effect = (g.effect_class or "read_only").strip().lower()
    effect_badge = {
        "read_only": "opened",
        "write": "pending",
        "external_side_effect": "pending",
        "destructive": "failed",
        "privileged": "failed",
    }.get(effect, "cancelled")
    return {
        "id": g.id,
        "handle": g.handle,
        "handle_short": g.handle[:10] + "…",
        "principal_id": g.principal_id,
        "principal_short": g.principal_id[:10] + "…",
        "effect_class": effect,
        "effect_badge": effect_badge,
        "actions": actions,
        "actions_title": " · ".join(actions),
        "status": status,
        "raw_status": g.status,
        "created": _fmt_dt(created),
        "created_rel": _rel_dt(created),
        "expires": _fmt_dt(expires),
        "expires_rel": _rel_dt(expires),
        "expires_soon": expires_soon,
        "revoked_at": _fmt_dt(revoked),
        "last_used": _fmt_dt(last_used),
        "last_used_rel": _rel_dt(last_used),
    }


def _page_window(current: int, total: int, span: int = 1) -> list[int | None]:
    """Page numbers to render in the pager (None renders as an ellipsis).
    Always shows the first and last page plus a window around current."""
    if total <= 7:
        return list(range(1, total + 1))
    shown: set[int] = {1, total, current}
    for delta in range(1, span + 1):
        shown.update({current - delta, current + delta})
    numbers = sorted(p for p in shown if 1 <= p <= total)
    window: list[int | None] = []
    previous = 0
    for p in numbers:
        if p - previous > 1:
            window.append(None)
        window.append(p)
        previous = p
    return window


_CSV_HEADER = (
    "id",
    "status",
    "kind",
    "purpose",
    "origin_reference",
    "created_at_utc",
    "opened_at_utc",
    "completed_at_utc",
    "challenge_expires_at_utc",
    "evidence_material_type",
    "failure_reason_safe",
)


def _csv_cell(value: Any) -> str:
    """Neutralize spreadsheet formula injection: a cell beginning with
    =, +, - or @ would be evaluated by common spreadsheet apps."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@"):
        return "'" + text
    return text


def _handoffs_csv(views: list[dict[str, Any]]) -> str:
    """Render handoff VIEWS as metadata-only CSV (no secret material)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_CSV_HEADER)
    for v in views:
        evidence = v.get("evidence") or {}
        writer.writerow(
            [
                _csv_cell(v.get("id")),
                _csv_cell(v.get("display_status") or v.get("status")),
                _csv_cell(v.get("kind")),
                _csv_cell(v.get("purpose")),
                _csv_cell(v.get("origin_reference")),
                _csv_cell(v.get("created_at")),
                _csv_cell(v.get("opened_at")),
                _csv_cell(v.get("completed_at")),
                _csv_cell(v.get("expires_at")),
                _csv_cell(evidence.get("material_type", "")),
                _csv_cell(v.get("failure_reason")),
            ]
        )
    return buffer.getvalue()


_AUDIT_CSV_HEADER = (
    "id",
    "created_at_utc",
    "event_kind",
    "outcome",
    "actor_kind",
    "client_ip",
    "handoff_id",
    "grant_id",
    "reason",
    "removed",
    "reclaimed_bytes",
    "payload_json",
)


def _audit_csv(rows: list[AuditEvent]) -> str:
    """Render audit rows as metadata-only CSV.

    Common payload fields get their own columns (sortable in a
    spreadsheet); the full payload rides along as JSON. Payloads are
    metadata-only by construction — the no-secret guarantee is pinned by
    tests, not by this function's optimism.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_AUDIT_CSV_HEADER)
    for e in rows:
        payload = e.payload if isinstance(e.payload, dict) else {}
        removed = payload.get("removed")
        reclaimed = payload.get("reclaimed_bytes")
        writer.writerow(
            [
                _csv_cell(e.id),
                _csv_cell(_fmt_dt(_aware(e.created_at))),
                _csv_cell(e.event_kind),
                _csv_cell(e.outcome),
                _csv_cell(e.actor_kind),
                _csv_cell(payload.get("client_ip", "")),
                _csv_cell(payload.get("handoff_id", "")),
                _csv_cell(payload.get("grant_id", "")),
                _csv_cell(payload.get("reason", "")),
                _csv_cell("" if removed is None else removed),
                _csv_cell("" if reclaimed is None else reclaimed),
                _csv_cell(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else ""
                ),
            ]
        )
    return buffer.getvalue()


_GRANTS_CSV_HEADER = (
    "id",
    "principal_id",
    "effect_class",
    "status",
    "created_at_utc",
    "expires_at_utc",
    "revoked_at_utc",
    "last_used_at_utc",
    "allowed_actions_count",
)


def _grants_csv(views: list[dict[str, Any]], action_counts: list[int]) -> str:
    """Render grant VIEWS as metadata-only CSV.

    DELIBERATELY WITHOUT the handle column: the handle is the live
    authority token, and an operator convenience export must never become
    the leak path for it (pinned by test)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(_GRANTS_CSV_HEADER)
    for v, count in zip(views, action_counts, strict=True):
        writer.writerow(
            [
                _csv_cell(v.get("id")),
                _csv_cell(v.get("principal_id")),
                _csv_cell(v.get("effect_class")),
                _csv_cell(v.get("status")),
                _csv_cell(v.get("created")),
                _csv_cell(v.get("expires")),
                _csv_cell(v.get("revoked_at")),
                _csv_cell(v.get("last_used")),
                _csv_cell(count),
            ]
        )
    return buffer.getvalue()


def _parse_gc_flash(value: str) -> dict[str, Any] | None:
    """Parse the ?gc= redirect flash into a displayable result."""
    if not value:
        return None
    if value == "error":
        return {"kind": "error", "message": "The maintenance request was rejected (stale token)."}
    if value == "unavailable":
        return {"kind": "error", "message": "The blob store is not available in this deployment."}
    if ":" in value:
        removed_s, bytes_s = value.split(":", 1)
        if removed_s.isdigit() and bytes_s.isdigit():
            return {"kind": "ok", "removed": int(removed_s), "reclaimed_bytes": int(bytes_s)}
    return None


def _parse_bulk_flash(value: str) -> dict[str, Any] | None:
    """Parse the ?bulk= redirect flash into a displayable summary."""
    if not value:
        return None
    if value == "none":
        return {"kind": "info", "message": "No handoffs were selected, so nothing was cancelled."}
    if value == "stale_token":
        return {
            "kind": "error",
            "message": "The bulk-cancel confirmation was stale — reload and select again.",
        }
    cancelled = failed = None
    for part in value.split(","):
        if part.startswith("cancelled:"):
            with suppress(ValueError):
                cancelled = int(part.split(":", 1)[1])
        elif part.startswith("failed:"):
            with suppress(ValueError):
                failed = int(part.split(":", 1)[1])
    if cancelled is None and failed is None:
        return None
    return {"kind": "ok", "cancelled": cancelled or 0, "failed": failed or 0}


def _audit_view(e: AuditEvent) -> dict[str, Any]:
    """Display projection of one audit event: a human sentence built from
    the safe payload (identities shortened), plus color hints for the UI."""
    payload = e.payload if isinstance(e.payload, dict) else {}
    kind = e.event_kind
    outcome = e.outcome if e.outcome in ("success", "denied", "failed") else "success"

    def _tail(value: Any) -> str:
        text = str(value or "")
        return text[-8:] if len(text) > 8 else text

    detail: str
    if kind == "control.login":
        if outcome == "success":
            detail = "Signed in with the access token"
        elif payload.get("reason") == "rate_limited":
            detail = "Sign-in refused — too many failed attempts (rate limited)"
        else:
            detail = "Sign-in refused — invalid access token"
    elif kind == "control.logout":
        detail = "Signed out"
    elif kind == "control.handoff_submitted":
        hid = str(payload.get("handoff_id") or "")
        if outcome == "success":
            detail = (
                f"Submitted {payload.get('material_type', 'material')} for handoff …{_tail(hid)}"
            )
        elif payload.get("reason") == "stale_csrf":
            detail = f"Submit refused — stale form token (handoff …{_tail(hid)})"
        else:
            detail = f"Submit failed — {payload.get('reason', 'unknown')}"
    elif kind == "control.handoff_cancelled":
        hid = str(payload.get("handoff_id") or "")
        if outcome == "success":
            detail = f"Cancelled handoff …{_tail(hid)}"
            reason = str(payload.get("reason") or "")
            if reason and reason != "bulk_cancel":
                detail += f" — {reason[:120]}"
            elif reason == "bulk_cancel":
                detail += " (bulk cancel)"
        else:
            detail = f"Cancel failed — {payload.get('reason', 'unknown')}"
    elif kind == "control.grant_revoked":
        gid = str(payload.get("grant_id") or "")
        if outcome == "success":
            detail = f"Revoked authority grant …{_tail(gid)}"
        else:
            detail = "Revoke failed — unknown grant"
    elif kind == "control.blob_gc":
        removed = payload.get("removed")
        kb = (payload.get("reclaimed_bytes") or 0) / 1024
        detail = f"Blob GC removed {removed} object(s), reclaimed {kb:.1f} KB"
    elif kind == "system.blob_gc":
        removed = payload.get("removed")
        kb = (payload.get("reclaimed_bytes") or 0) / 1024
        detail = f"Scheduled GC removed {removed} object(s), reclaimed {kb:.1f} KB"
    else:
        detail = kind

    # color family for the kind chip
    if kind.startswith("control.login") or kind == "control.logout":
        family = "auth"
    elif kind.startswith("control.handoff"):
        family = "handoff"
    elif kind.startswith("control.grant"):
        family = "grant"
    else:
        family = "maintenance"

    created = e.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True) if payload else ""
    copy_text = f"{kind} · {outcome} · {_fmt_dt(created) or ''} · {detail}"
    if payload_json:
        copy_text += f" · {payload_json}"
    return {
        "kind": kind,
        "family": family,
        "outcome": outcome,
        "detail": detail,
        "client_ip": str(payload.get("client_ip") or "—"),
        "actor_kind": e.actor_kind,
        "system": e.actor_kind == "system",
        "created": _fmt_dt(created),
        "created_rel": _rel_dt(created),
        "payload_json": payload_json,
        "copy_text": copy_text,
    }


def _runtime_counters(app) -> list[tuple[str, float]]:
    """Dashboard runtime-activity rows: (label, value) for counters that
    exist. Aggregation sums label variants so the card stays compact."""
    metrics = getattr(getattr(app.state, "services", None), "metrics", None)
    if metrics is None:
        return []
    try:
        totals = metrics.snapshot_counters()
    except Exception:
        log.warning("control.metrics_snapshot_failed")
        return []
    rows: list[tuple[str, float]] = []
    for name, label in _DASHBOARD_COUNTERS:
        value = totals.get(name)
        if value:
            rows.append((label, value))
    return rows


def _blob_stats(app) -> dict[str, int] | None:
    """Blob store aggregates for the storage card; None when absent."""
    blob_store = getattr(getattr(app.state, "services", None), "blob_store", None)
    if blob_store is None:
        return None
    try:
        return blob_store.stats()
    except OSError:
        log.warning("control.blob_stats_failed")
        return None


def view_kind(handoff: HumanHandoffRecord) -> str:
    if isinstance(handoff.requested_actions_json, dict):
        return str(handoff.requested_actions_json.get("handoff_kind", "secret"))
    return "secret"


def _is_expired(h: HumanHandoffRecord) -> bool:
    """True when the challenge deadline passed while the handoff was
    still actionable (completed/failed handoffs are never 'expired')."""
    if h.status not in OPEN_STATUSES:
        return False
    expires = h.challenge_expires_at
    if expires is None:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= datetime.now(UTC)


def _material_choices(kind: str) -> dict[str, str]:
    if kind == "browser":
        return {
            MaterialType.BROWSER_SESSION_REFERENCE.value: _MATERIAL_LABELS[
                MaterialType.BROWSER_SESSION_REFERENCE.value
            ]
        }
    return {
        MaterialType.OPAQUE_SECRET.value: _MATERIAL_LABELS[MaterialType.OPAQUE_SECRET.value],
        MaterialType.SESSION_MATERIAL.value: _MATERIAL_LABELS[MaterialType.SESSION_MATERIAL.value],
        MaterialType.DELEGATED_GRANT.value: _MATERIAL_LABELS[MaterialType.DELEGATED_GRANT.value],
    }


def _handoff_view(h: HumanHandoffRecord) -> dict[str, Any]:
    actions: list[str] = []
    kind = "secret"
    if isinstance(h.requested_actions_json, dict):
        kind = str(h.requested_actions_json.get("handoff_kind", "secret"))
        raw_actions = h.requested_actions_json.get("actions") or []
        for a in raw_actions:
            if isinstance(a, dict):
                actions.append(str(a.get("description", "")))
            else:
                actions.append(str(a))
    expired = _is_expired(h)
    return {
        "id": h.id,
        "purpose": h.purpose,
        "kind": kind,
        "status": h.status,
        # Display state: an expired-but-not-yet-marked handoff renders as
        # "expired" everywhere, even though the stored status still says
        # pending/opened (that is flipped lazily by the broker).
        "display_status": "expired" if expired else h.status,
        "expired": expired,
        "open": h.status in OPEN_STATUSES,
        "submittable": h.status in OPEN_STATUSES and not expired,
        "instructions": h.instructions_text or "",
        "origin_reference": h.origin_reference or "",
        "actions": [a for a in actions if a],
        "created_at": _fmt_dt(h.created_at_col or h.created_at),
        "created_rel": _rel_dt(h.created_at_col or h.created_at),
        "opened_at": _fmt_dt(h.opened_at),
        "expires_at": _fmt_dt(h.challenge_expires_at),
        "completed_at": _fmt_dt(h.completed_at),
        "evidence": h.completion_evidence_json or {},
        "failure_reason": h.failure_reason_safe or "",
    }


def _fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _rel_dt(dt: datetime | None) -> str | None:
    """Coarse relative time for table rows ("4m ago", future: "in 23h")."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = (datetime.now(UTC) - dt).total_seconds()
    if delta < 0:
        # future timestamp (grant expiry) — never clamp to "just now",
        # an operator must not misread an expiry that hasn't happened.
        # Rounds UP so "in 2h" is honest even at 1h59m remaining.
        ahead = -delta
        if ahead < 60:
            return "in a moment"
        if ahead < 3600:
            return f"in {int(ahead // 60) + 1}m"
        if ahead < 86400:
            return f"in {int(ahead // 3600) + 1}h"
        return f"in {int(ahead // 86400) + 1}d"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"
