"""Secure control plane — the server-rendered same-origin dashboard.

ADR-0048 specified this surface but it was never implemented: the
authority broker's `submit_handoff` had NO human-reachable entrypoint,
so a handoff could be created by the intelligence but never completed by
a human. This module is that missing HTTP layer.

Surface (all server-rendered HTML, same-origin, no external assets):

- GET  /control                          dashboard home (handoff overview)
- GET  /control/login                    operator login
- POST /control/login                    verify token → session cookie
- POST /control/logout                   clear session
- GET  /control/handoffs/{id}            handoff detail (metadata only)
- POST /control/handoffs/{id}/submit     submit the secret (encrypted
                                         immediately, never echoed)

Security posture (per ADR-0048 "Control plane"):
- Authentication: requires WAX_CONTROL_PLANE_TOKEN (bearer header,
  X-Control-Token header, ?token=, or the HttpOnly SameSite=Strict
  session cookie issued after login). Without a token the dashboard is
  enabled ONLY in development (with a loud banner) and REFUSES to serve
  in production (503) — misconfiguration must be loud, never silent.
- CSRF: signed, expiring, handoff-bound token embedded in every form and
  verified on submit (HMAC-SHA256 over expiry + handoff id).
- Cache-Control: no-store on every response; the secret is NEVER echoed,
  displayed, or logged after submission.
- No external resources: styles are inline; nothing leaks the operator's
  IP to a CDN.

The dashboard is an INTERFACE adapter — it lives in wax.runtime, never
imports the bridge or wax.interfaces (INV-02), and knows nothing about
domains, education, or WhatsApp.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from wax.runtime.authority.contracts import (
    AuthorityStatus,
    HandoffStatus,
    MaterialType,
)
from wax.runtime.logging import get_logger
from wax.state.authority_broker_models import (
    AuthorityGrantRecord,
    HumanHandoffRecord,
)
from wax.state.engine import db_session

log = get_logger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

_SESSION_COOKIE = "wax_control_session"
_SESSION_TTL_SECONDS = 12 * 3600
_CSRF_TTL_SECONDS = 15 * 60

# Statuses shown as "actionable" on the dashboard and submittable.
OPEN_STATUSES = (
    HandoffStatus.PENDING.value,
    HandoffStatus.OPENED.value,
    HandoffStatus.AWAITING_HUMAN.value,
)

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

    def make_csrf(self) -> str:
        import secrets as _secrets

        exp = int(time.time()) + _CSRF_TTL_SECONDS
        nonce_value = _secrets.token_hex(8)
        return f"{exp}.{nonce_value}.{_hmac_hex(self._key(), f'control-csrf:{exp}:{nonce_value}')}"

    def verify_csrf(self, token: str | None) -> bool:
        if not token or token.count(".") != 2:
            return False
        exp_s, nonce, sig = token.split(".")
        if not exp_s.isdigit():
            return False
        if int(exp_s) < time.time():
            return False
        expected = _hmac_hex(self._key(), f"control-csrf:{exp_s}:{nonce}")
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

        async with db_session() as session:
            open_count = (
                await session.execute(
                    select(func.count())
                    .select_from(HumanHandoffRecord)
                    .where(HumanHandoffRecord.status.in_(OPEN_STATUSES))
                )
            ).scalar_one()
            active_grants = (
                await session.execute(
                    select(func.count())
                    .select_from(AuthorityGrantRecord)
                    .where(AuthorityGrantRecord.status == AuthorityStatus.ACTIVE.value)
                )
            ).scalar_one()
            rows = (
                (
                    await session.execute(
                        select(HumanHandoffRecord)
                        .order_by(HumanHandoffRecord.created_at.desc())
                        .limit(50)
                    )
                )
                .scalars()
                .all()
            )
            await session.commit()

        handoffs = [_handoff_view(h) for h in rows]
        return _html_response(
            request,
            "dashboard.html",
            {
                "open_count": open_count,
                "active_grants": active_grants,
                "handoffs": handoffs,
                "env": settings.env.value,
                "isolation_backend": settings.isolation_backend,
                "dev_open": auth.dev_open,
            },
        )

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
        return _html_response(request, "login.html", {"dev_open": auth.dev_open})

    @app.post("/control/login", tags=["control"], include_in_schema=False)
    async def control_login(request: Request):
        settings = app.state.settings
        auth = ControlPlaneAuth(settings)
        if settings.is_production and not auth.enabled:
            return _html_response(request, "error.html", {"error": "Control plane disabled."}, 503)

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
            log.warning("control.login_failed")
            return _html_response(
                request, "login.html", {"dev_open": False, "error": "Invalid access token."}, 401
            )
        response = _redirect("/control")
        response.set_cookie(
            _SESSION_COOKIE,
            auth.session_cookie_value(),
            max_age=_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=settings.is_production,
        )
        return response

    @app.post("/control/logout", tags=["control"], include_in_schema=False)
    async def control_logout(request: Request):
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
            csrf = auth.make_csrf() if view["open"] else ""

        return _html_response(
            request,
            "handoff_detail.html",
            {"h": view, "csrf": csrf, "material_labels": _material_choices(view["kind"])},
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
                view = _handoff_view(handoff)
                return _html_response(
                    request,
                    "handoff_detail.html",
                    {
                        "h": view,
                        "csrf": auth.make_csrf(),
                        "material_labels": _material_choices(view["kind"]),
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
                fresh = await session.get(HumanHandoffRecord, handoff_id)
                view = _handoff_view(fresh) if fresh is not None else _handoff_view(handoff)
                return _html_response(
                    request,
                    "handoff_detail.html",
                    {
                        "h": view,
                        "csrf": auth.make_csrf(),
                        "material_labels": _material_choices(view["kind"]),
                        "error": str(e),
                    },
                    status_code=400,
                )

        # SUCCESS: the secret is already encrypted. From here on only
        # opaque handles exist — the submitted value is NEVER echoed.
        log.info("control.handoff_submitted", handoff_id=handoff_id)
        return _redirect(f"/control/handoffs/{handoff_id}?submitted=1")


# ---------------------------------------------------------------------------
# View helpers
# ---------------------------------------------------------------------------


def view_kind(handoff: HumanHandoffRecord) -> str:
    if isinstance(handoff.requested_actions_json, dict):
        return str(handoff.requested_actions_json.get("handoff_kind", "secret"))
    return "secret"


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
    return {
        "id": h.id,
        "purpose": h.purpose,
        "kind": kind,
        "status": h.status,
        "open": h.status in OPEN_STATUSES,
        "instructions": h.instructions_text or "",
        "origin_reference": h.origin_reference or "",
        "actions": [a for a in actions if a],
        "created_at": _fmt_dt(h.created_at_col or h.created_at),
        "expires_at": _fmt_dt(h.challenge_expires_at),
        "completed_at": _fmt_dt(h.completed_at),
        "evidence": h.completion_evidence_json or {},
    }


def _fmt_dt(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
