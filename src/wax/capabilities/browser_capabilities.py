"""Browser handoff capability (P0-Browser).

A BROWSER handoff is a human handoff where the human completes a step IN
A BROWSER — logging into a site, solving a captcha, clicking a consent
dialog — and then hands the runtime a BROWSER SESSION REFERENCE (an
opaque, encrypted descriptor of the authenticated session).

Why a capability instead of a headless browser: the runtime deliberately
does NOT drive a browser with the human's credentials. Headless-browser
automation under model control would put the human's authenticated
identity inside the model's action radius. Instead, the model declares
WHY it needs a browser session and WHAT for; the human performs the
browser step themselves and submits the session reference through the
secure control plane (ADR-0048). The reference is encrypted immediately
(AES-GCM) and the model receives only an opaque authority handle.

This is the `MaterialType.BROWSER_SESSION_REFERENCE` path — an enum value
that previously existed with no producer and no consumer. Now:
- producer: the human, via control plane submit on a `browser` handoff
- consumer: any capability holding the authority grant handle (the same
  handle-based consumption as opaque secrets)

Design principle: The runtime provides affordances, not workflows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from wax.capabilities.contracts import CapabilityDescriptor
from wax.capabilities.registry import CapabilityRegistry

if TYPE_CHECKING:
    from wax.runtime.services import RuntimeServices

BROWSER_HANDOFF_DESCRIPTOR = CapabilityDescriptor(
    name="browser.handoff",
    description=(
        "Request a HUMAN to complete a browser step (login, captcha, "
        "consent, 2FA) on your behalf and hand back a browser session "
        "reference. Use this when a discovered path requires an "
        "authenticated browser session you cannot obtain through the "
        "credential vault. Describe WHAT the human should do and WHY — "
        "never ask for credentials directly in text. The human performs "
        "the step themselves in their own browser; the runtime encrypts "
        "the session reference immediately and you receive only an "
        "opaque handoff_ref. Poll authority.status until completed."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "purpose": {
                "type": "string",
                "maxLength": 500,
                "description": "Safe explanation of why a browser session is needed",
            },
            "origin_reference": {
                "type": "string",
                "description": "The URL/site the human should work with",
            },
            "requested_actions": {
                "type": "array",
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "effect_class": {
                            "type": "string",
                            "enum": [
                                "read_only",
                                "write",
                                "external_side_effect",
                                "destructive",
                                "privileged",
                            ],
                        },
                    },
                    "required": ["description"],
                },
            },
            "instructions": {
                "type": "string",
                "maxLength": 4000,
                "description": (
                    "Concrete, safe instructions for the human: which site, "
                    "which account, which step. NEVER include credentials."
                ),
            },
            "expires_at": {"type": "string"},
        },
        "required": ["purpose"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "handoff_ref": {"type": "string"},
            "expires_at": {"type": "string"},
            "control_plane_path": {"type": "string"},
        },
    },
    required_permission="capability.invoke:credential",
    timeout_seconds=10.0,
    idempotent=True,
    is_destructive=False,
    sensitive_inputs=(),  # NO secret in the input — that is the whole point
)


def register_browser_capabilities(registry: CapabilityRegistry, services: RuntimeServices) -> None:
    """Register the browser handoff capability on the registry."""

    async def browser_handoff_impl(inputs: dict[str, Any], ctx: Any) -> dict[str, Any]:
        from wax.runtime.authority import AuthorityRequest
        from wax.state.engine import db_session

        if services.authority_broker is None:
            raise ValueError("authority broker not configured")

        purpose = inputs.get("purpose", "")
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("purpose is required")
        instructions = inputs.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise ValueError("instructions must be a string")
        requested_actions = inputs.get("requested_actions", [])
        if not isinstance(requested_actions, list):
            raise ValueError("requested_actions must be an array")

        request = AuthorityRequest(
            purpose=purpose,
            origin_reference=inputs.get("origin_reference"),
            requested_actions=requested_actions,
            human_required=True,
            # P0-Browser: mark the handoff so the control plane renders a
            # browser-session form and submission stores a
            # browser_session_reference material (not an opaque secret).
            handoff_kind="browser",
            instructions_text=instructions,
        )

        async with db_session() as session:
            result = await services.authority_broker.request_authority(
                session,
                principal_id=ctx.principal_id,
                request=request,
                execution_id=ctx.request_id or ctx.execution_id,
            )
            await session.commit()

        return {
            "status": result.status,
            "handoff_ref": result.handoff_ref,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "control_plane_path": f"/control/handoffs/{result.handoff_ref}"
            if result.handoff_ref
            else None,
        }

    registry.register(BROWSER_HANDOFF_DESCRIPTOR, browser_handoff_impl)
