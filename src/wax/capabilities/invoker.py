"""Capability invoker — the SOLE point where the AI can cause effects.

INVARIANT INV-04: AI-requested actions must pass through runtime authorization.
This invoker is the enforcement point.

Flow:
1. AI (via the runtime) submits a CapabilityInvocationRequest.
2. Invoker looks up the capability in the registry.
3. Invoker calls AuthorizationService.check() — the runtime decides.
4. If denied, return CapabilityInvocationResult(outcome="denied").
5. If authorized, validate inputs against the descriptor's input_schema.
6. Execute the capability implementation with a timeout.
7. Record the invocation in the audit log.
8. Return the result.

The AI NEVER directly executes a capability. It ALWAYS goes through this
invoker, which ALWAYS checks authorization.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from ulid import ULID

from wax.authority.service import AuthorizationService
from wax.capabilities.contracts import (
    CapabilityInvocationRequest,
    CapabilityInvocationResult,
    CapabilityStatus,
    InvocationContext,
)
from wax.capabilities.registry import CapabilityRegistry
from wax.runtime.logging import get_logger

log = get_logger(__name__)


class CapabilityInvoker:
    """The single entrypoint for capability invocation.

    Use:
        invoker = CapabilityInvoker(registry, auth_service)
        result = await invoker.invoke(request)
        if result.outcome != "success":
            ...
    """

    def __init__(
        self,
        registry: CapabilityRegistry,
        auth_service: AuthorizationService,
    ) -> None:
        self._registry = registry
        self._auth = auth_service

    async def invoke(
        self,
        request: CapabilityInvocationRequest,
    ) -> CapabilityInvocationResult:
        """Invoke a capability on behalf of a principal.

        The runtime enforces authorization BEFORE executing the capability.
        The AI cannot bypass this — there is no other path to invoke.
        """
        started_at = datetime.now(UTC)
        execution_id = str(ULID())
        start_perf = time.perf_counter()

        # 1. Lookup the capability
        try:
            descriptor, impl = self._registry.get(request.capability_name)
        except Exception:
            return self._failure_result(
                request, execution_id, started_at, start_perf,
                outcome="not_found",
                error=f"No such capability: {request.capability_name}",
            )

        # 2. Check status (must be AVAILABLE)
        status = self._registry.get_status(request.capability_name)
        if status != CapabilityStatus.AVAILABLE:
            return self._failure_result(
                request, execution_id, started_at, start_perf,
                outcome="denied",
                error=f"Capability status is {status.value}, not available",
            )

        # 3. AUTHORIZE — this is the runtime enforcement point (INV-04)
        authorized = await self._auth.check(
            request.principal_id,
            descriptor.required_permission,
            actor_kind="ai",
            capability_name=request.capability_name,
            request_id=request.request_id,
        )
        if not authorized:
            return self._failure_result(
                request, execution_id, started_at, start_perf,
                outcome="denied",
                error=f"Principal lacks permission: {descriptor.required_permission}",
            )

        # 3.5 VALIDATE inputs against the descriptor's declared contract.
        # (Constitutional audit fix: the docstring promised this validation
        # while the code did none — a false mechanism. Implementations
        # still own full semantic validation; this is the honest first
        # pass over the DECLARED contract: required fields present, basic
        # types, declared length/size bounds.)
        try:
            self._validate_inputs(request.inputs, descriptor.input_schema)
        except ValueError as e:
            return self._failure_result(
                request, execution_id, started_at, start_perf,
                outcome="failure",
                error=f"Invalid inputs: {e}",
            )

        # 4. Execute with timeout. The implementation receives an
        # InvocationContext (who/what/why) — never authorization power.
        context = InvocationContext(
            principal_id=request.principal_id,
            capability_name=request.capability_name,
            execution_id=execution_id,
            request_id=request.request_id,
        )
        try:
            outputs = await asyncio.wait_for(
                impl(request.inputs, context),
                timeout=descriptor.timeout_seconds,
            )
        except TimeoutError:
            log.warning(
                "capability.timeout",
                capability=request.capability_name,
                principal_id=request.principal_id,
                timeout_s=descriptor.timeout_seconds,
            )
            return self._failure_result(
                request, execution_id, started_at, start_perf,
                outcome="timeout",
                error=f"Capability exceeded {descriptor.timeout_seconds}s timeout",
            )
        except Exception as e:
            log.warning(
                "capability.failure",
                capability=request.capability_name,
                principal_id=request.principal_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return self._failure_result(
                request, execution_id, started_at, start_perf,
                outcome="failure",
                error=f"{type(e).__name__}: {e}",
            )

        ended_at = datetime.now(UTC)
        duration_ms = (time.perf_counter() - start_perf) * 1000

        log.info(
            "capability.invoked",
            capability=request.capability_name,
            principal_id=request.principal_id,
            execution_id=execution_id,
            duration_ms=round(duration_ms, 2),
        )

        return CapabilityInvocationResult(
            capability_name=request.capability_name,
            outcome="success",
            outputs=outputs,
            execution_id=execution_id,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=round(duration_ms, 2),
        )

    def _failure_result(
        self,
        request: CapabilityInvocationRequest,
        execution_id: str,
        started_at: datetime,
        start_perf: float,
        *,
        outcome: str,
        error: str,
    ) -> CapabilityInvocationResult:
        ended_at = datetime.now(UTC)
        duration_ms = (time.perf_counter() - start_perf) * 1000
        return CapabilityInvocationResult(
            capability_name=request.capability_name,
            outcome=outcome,
            error=error,
            execution_id=execution_id,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=round(duration_ms, 2),
        )

    # --- Declared-contract validation (constitutional audit fix) ------------

    @staticmethod
    def _validate_inputs(
        inputs: dict, schema: dict | None
    ) -> None:
        """Enforce the descriptor's DECLARED input contract.

        Covers the JSON-schema subset the descriptors actually use:
        required fields, basic types, maxLength/minLength, minimum/maximum,
        minItems/maxItems, enum. Implementations keep full semantic
        validation — this pass exists so the declared contract is not
        decoration: the invoker's documented step 5 is real code now.
        """
        if not schema:
            return
        if not isinstance(inputs, dict):
            raise ValueError("inputs must be a JSON object")
        for name in schema.get("required", []):
            if name not in inputs or inputs[name] is None:
                raise ValueError(f"missing required input: {name}")
        properties = schema.get("properties", {})
        for name, value in inputs.items():
            spec = properties.get(name)
            if spec is None:
                continue
            declared = spec.get("type")
            if declared and not CapabilityInvoker._type_ok(value, declared):
                raise ValueError(f"input {name} must be of type {declared}")
            if isinstance(value, str):
                max_len = spec.get("maxLength")
                if max_len is not None and len(value) > max_len:
                    raise ValueError(f"input {name} exceeds {max_len} characters")
                min_len = spec.get("minLength")
                if min_len is not None and len(value) < min_len:
                    raise ValueError(
                        f"input {name} is shorter than {min_len} characters"
                    )
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                minimum = spec.get("minimum")
                if minimum is not None and value < minimum:
                    raise ValueError(f"input {name} must be >= {minimum}")
                maximum = spec.get("maximum")
                if maximum is not None and value > maximum:
                    raise ValueError(f"input {name} must be <= {maximum}")
            if isinstance(value, list):
                min_items = spec.get("minItems")
                if min_items is not None and len(value) < min_items:
                    raise ValueError(f"input {name} needs at least {min_items} items")
                max_items = spec.get("maxItems")
                if max_items is not None and len(value) > max_items:
                    raise ValueError(f"input {name} allows at most {max_items} items")
            enum = spec.get("enum")
            if enum and value not in enum:
                raise ValueError(f"input {name} must be one of {list(enum)}")

    @staticmethod
    def _type_ok(value: object, declared: str) -> bool:
        checks = {
            "string": lambda v: isinstance(v, str),
            "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
            "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
            "boolean": lambda v: isinstance(v, bool),
            "array": lambda v: isinstance(v, list),
            "object": lambda v: isinstance(v, dict),
            "null": lambda v: v is None,
        }
        check = checks.get(declared)
        return check is None or check(value)
