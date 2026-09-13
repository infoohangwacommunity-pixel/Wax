"""code.run — isolated code execution capability (Phase U).

Everything dangerous crosses explicit runtime authority. This capability
wraps the IsolationService/SubprocessBoundary so the AI can RUN code in an
ephemeral, environment-scrubbed subprocess with timeout + output caps.

Trust boundary (honest, stated in the descriptor the model sees):
- A subprocess is NOT a sandbox against malicious code. It isolates against
  accidents (crashes, hangs, env leakage). The permission
  `capability.invoke:code_run` is deliberately NOT in the member role —
  code execution is opt-in per deployment via role assignment, and the
  descriptor documents the boundary to the model.

Optional integration with Phase S: pass workspace_resource_id (from
scratch.workspace) to run inside a provisioned ephemeral directory — the
resource is verified as owned by the caller before use.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from wax.capabilities.contracts import CapabilityDescriptor, InvocationContext
from wax.capabilities.registry import CapabilityRegistry
from wax.runtime.logging import get_logger
from wax.runtime.services import RuntimeServices

log = get_logger(__name__)

MAX_CODE_CHARS = 20_000
MAX_TIMEOUT_SECONDS = 30.0

CODE_RUN_DESCRIPTOR = CapabilityDescriptor(
    name="code.run",
    description=(
        "Run Python or shell code in an isolated subprocess (scrubbed "
        "environment, hard timeout, output caps). NOTE: a subprocess is "
        "isolation against accidents, not a sandbox against malicious "
        "code. Requires the code_run permission."
    ),
    version="1.0.0",
    input_schema={
        "type": "object",
        "properties": {
            "code": {"type": "string", "maxLength": MAX_CODE_CHARS},
            "language": {"type": "string", "enum": ["python", "shell"]},
            "timeout_seconds": {
                "type": "number",
                "minimum": 0.5,
                "maximum": MAX_TIMEOUT_SECONDS,
                "default": 10.0,
            },
            "workspace_resource_id": {
                "type": "string",
                "description": "Optional scratch.workspace resource id to use as cwd",
            },
        },
        "required": ["code"],
    },
    output_schema={
        "type": "object",
        "properties": {
            "exit_code": {"type": "integer"},
            "stdout": {"type": "string"},
            "stderr": {"type": "string"},
            "timed_out": {"type": "boolean"},
            "truncated": {"type": "boolean"},
            "duration_ms": {"type": "number"},
        },
    },
    required_permission="capability.invoke:code_run",
    timeout_seconds=35.0,  # capability wrapper > max subprocess timeout
    idempotent=True,
    is_destructive=False,  # isolation handles the danger; authority gates access
)


def register_code_run_capability(registry: CapabilityRegistry, services: RuntimeServices) -> None:
    """Register code.run bound to this container."""

    async def code_run_impl(inputs: dict[str, Any], ctx: InvocationContext) -> dict[str, Any]:

        from wax.isolation.contracts import IsolationRequest
        from wax.isolation.service import IsolationService
        from wax.resources.contracts import ResourceKind, ResourceUsage
        from wax.state.engine import db_session

        code = inputs.get("code")
        if not code or not isinstance(code, str):
            raise ValueError("code is required")
        if len(code) > MAX_CODE_CHARS:
            raise ValueError(f"code exceeds {MAX_CODE_CHARS} characters")
        language = inputs.get("language", "python")
        if language not in ("python", "shell"):
            raise ValueError("language must be 'python' or 'shell'")
        try:
            timeout_seconds = float(inputs.get("timeout_seconds", 10.0))
        except (TypeError, ValueError) as e:
            raise ValueError("timeout_seconds must be a number") from e
        if not 0.5 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout_seconds must be between 0.5 and {MAX_TIMEOUT_SECONDS}")

        working_dir = None
        workspace_resource_id = inputs.get("workspace_resource_id")
        if workspace_resource_id:
            from datetime import datetime

            from sqlalchemy import select

            from wax.state.provisioning_models import ProvisionedResourceRecord

            now = datetime.now(UTC)
            async with db_session() as session:
                result = await session.execute(
                    select(ProvisionedResourceRecord).where(
                        ProvisionedResourceRecord.id == workspace_resource_id,
                        ProvisionedResourceRecord.status == "active",
                    )
                )
                resource = result.scalar_one_or_none()
            resource_ok = (
                resource is not None
                and resource.principal_id == ctx.principal_id
                and resource.kind == "scratch_dir"
            )
            if resource_ok and resource.expires_at is not None:
                expires = resource.expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=UTC)
                resource_ok = expires > now
            if not resource_ok:
                raise ValueError(
                    "workspace_resource_id is not an active scratch_dir "
                    "owned by the requesting principal"
                )
            working_dir = resource.uri

        from wax.isolation.contracts import IsolationKind

        boundary = IsolationService.for_kind(IsolationKind.SUBPROCESS).boundary
        result = await boundary.execute(
            IsolationRequest(
                code=code,
                language=language,
                timeout_seconds=timeout_seconds,
                working_dir=working_dir,
            )
        )

        # Account the CPU/execution time actually spent.
        services.resource_accountant.try_consume(
            ResourceUsage(
                ctx.execution_id or f"code-run-{ctx.capability_name}",
                ResourceKind.CPU_SECONDS,
                max(result.duration_ms / 1000.0, 0.001),
                notes="code.run",
            )
        )

        return {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
            "duration_ms": result.duration_ms,
        }

    registry.register(CODE_RUN_DESCRIPTOR, code_run_impl)
