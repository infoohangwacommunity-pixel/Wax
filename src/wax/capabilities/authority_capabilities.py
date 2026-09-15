"""Authority capability descriptors + implementations (ADR-0048).

These are the model-facing capabilities that let the intelligence
request authority WITHOUT supplying a raw secret. The intelligence
calls authority.request with a purpose; the runtime creates a human
handoff; the user completes it through the control plane.

Design principle: The runtime provides affordances, not workflows.
The intelligence decides WHEN authority is needed.
"""

from __future__ import annotations

from wax.capabilities.contracts import CapabilityDescriptor

AUTHORITY_REQUEST_DESCRIPTOR = CapabilityDescriptor(
    name="authority.request",
    description=(
        "Request external authority for the current objective. The model "
        "NEVER supplies a raw secret — it declares a purpose and requested "
        "actions. The runtime creates a human handoff; the user completes "
        "it through the secure control plane. Returns a handoff_ref to poll. "
        "Use authority.status to check completion."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "purpose": {
                "type": "string",
                "maxLength": 500,
                "description": "Safe explanation of why authority is needed",
            },
            "origin_reference": {
                "type": "string",
                "description": "Discovered origin (e.g. a URL the intelligence found)",
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
            "expires_at": {"type": "string"},
            "human_required": {"type": "boolean", "default": True},
        },
        "required": ["purpose"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "handoff_ref": {"type": "string"},
            "expires_at": {"type": "string"},
        },
    },
    required_permission="capability.invoke:credential",
    timeout_seconds=10.0,
    idempotent=True,
    is_destructive=False,
    sensitive_inputs=(),  # NO secret in the input — that's the whole point
)

AUTHORITY_STATUS_DESCRIPTOR = CapabilityDescriptor(
    name="authority.status",
    description=(
        "Check the status of an authority request. Returns whether the "
        "handoff is pending, completed, or failed. After completion, "
        "returns an opaque authority_ref the intelligence can use. "
        "NEVER returns the secret value."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "handoff_ref": {"type": "string"},
        },
        "required": ["handoff_ref"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string"},
            "authority_ref": {"type": "string"},
            "expires_at": {"type": "string"},
        },
    },
    required_permission="capability.invoke:credential",
    timeout_seconds=5.0,
    idempotent=True,
    is_destructive=False,
)

AUTHORITY_REVOKE_DESCRIPTOR = CapabilityDescriptor(
    name="authority.revoke",
    description=(
        "Revoke an authority grant immediately. The grant and underlying "
        "encrypted material are marked revoked. Future use is denied."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "authority_ref": {"type": "string"},
        },
        "required": ["authority_ref"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "revoked": {"type": "boolean"},
        },
    },
    required_permission="capability.invoke:credential",
    timeout_seconds=10.0,
    idempotent=True,
    is_destructive=True,
)
