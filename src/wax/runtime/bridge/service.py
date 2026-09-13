"""RuntimeBridge — the canonical entrypoint for interface messages.

The bridge turns one interface message into one runtime execution, inside
runtime-enforced boundaries:

1. Idempotency: (interface_kind, interface_message_id) is processed at most
   once — unless the previous attempt FAILED, in which case redelivery
   retries the work (a failed message must not be a black hole). Terminal
   exhaustion marks the message dead and records a dead letter.
2. Identity: sender_interface_id → WAX Principal (created on first contact,
   assigned the default member role, credential marked used).
3. Security gate: rate limit → cost cap → abuse/input-sanitizer verdicts.
   Enforced by the runtime, never by the model.
4. Objective + Execution lifecycle: objective pending → in_progress →
   succeeded/failed; execution pending → running → succeeded/failed.
   The "work accepted" state is COMMITTED before the LLM call, so work
   survives process death and can be reconciled by the recovery scan.
5. Continuity: conversation open/resume/touch + recent memories compose the
   context the AI sees (ContinuityService is the single composer).
6. Resources: every execution gets a budget allocation from the
   ResourceAccountant; LLM calls consume it.
7. Observability: metrics + audit events on every outcome.

INVARIANTS:
- One interface message → at most one live execution (dedup via unique
  constraint + outcome state machine).
- The bridge never sees interface-specific shapes; it sees RuntimeRequest.
- The bridge never decides permissions; it delegates to Authority.
- The bridge never calls capability implementations directly; it goes
  through the CapabilityInvoker (the sole enforcement point, INV-04).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.authority.seed import DEFAULT_ROLE_FOR_NEW_PRINCIPALS, ensure_principal_role
from wax.capabilities.contracts import CapabilityInvocationResult
from wax.continuity.contracts import ContinuityContext
from wax.continuity.service import ContinuityService, ConversationService
from wax.core.exceptions import WaxStateConflictError
from wax.execution.contracts import ExecutionKind
from wax.execution.repository import ExecutionRepository
from wax.identity.contracts import ALLOWED_CREDENTIAL_KINDS
from wax.identity.repository import PrincipalRepository
from wax.intelligence.contracts import (
    LLMMessage,
    LLMRequest,
    MessageRole,
    ToolCall,
    ToolSpec,
)
from wax.intelligence.service import IntelligenceService
from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.objective.contracts import ObjectiveCreate, ObjectiveKind, ObjectiveStatus
from wax.objective.repository import ObjectiveRepository
from wax.observability.audit import record_audit_event
from wax.reliability.dead_letter import DeadLetterRepository
from wax.resources.contracts import ResourceKind, ResourceUsage
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeResponseStatus,
)
from wax.runtime.logging import get_logger
from wax.runtime.services import RuntimeServices
from wax.security.abuse import AbuseLevel
from wax.security.input_sanitizer import InjectionRisk, SanitizerResult
from wax.security.rate_limiter import RateLimitDecision
from wax.state.bridge_models import ProcessedMessageRecord
from wax.state.identity_models import Principal

log = get_logger(__name__)

# Map InterfaceKind → PrincipalCredential kind.
# This is the only place that knows that WhatsApp uses phone numbers and
# web uses session tokens. Adding a new interface means adding one line here.
_INTERFACE_CREDENTIAL_KIND: dict[InterfaceKind, str] = {
    InterfaceKind.WHATSAPP: "whatsapp_phone",
    InterfaceKind.WEB: "web_session",
    InterfaceKind.TELEGRAM: "telegram_chat",  # future
    InterfaceKind.API: "api_key",
}

# Outcome state machine for the idempotency record.
#
#   pending  → work accepted, execution in flight (or lost to a crash; the
#              recovery scan reconciles stale pendings)
#   success  → terminal; redelivery returns DUPLICATE
#   failed   → attempt failed; REDELIVERY RETRIES the work
#   dead     → attempts exhausted; terminal + dead-letter row
#   rate_limited / cost_capped / blocked / principal_unauthorized → terminal
#              rejections (redelivery returns DUPLICATE, no re-execution)
_RETRYABLE_OUTCOMES = frozenset({"failed"})
_TERMINAL_OUTCOMES = frozenset(
    {
        "success",
        "dead",
        "rate_limited",
        "cost_capped",
        "blocked",
        "principal_unauthorized",
        "internal_error",
    }
)

# How many full processing attempts one message gets before it is declared
# dead and written to the dead-letter table.
MAX_ATTEMPTS_PER_MESSAGE = 3

# Default per-execution budget. Deliberately generous for conversational
# work; the POINT is that limits exist and are enforced, not that they are
# tight. Per-principal cost/rate caps gate the loop upstream.
_DEFAULT_EXECUTION_BUDGET: dict[str, float] = {
    "execution_time_seconds": 300.0,
    "llm_tokens": 100_000.0,
    "llm_calls": 8.0,
    "capability_invocations": 10.0,
}


@dataclass(frozen=True)
class _Rejection:
    """A security-gate verdict that ends processing before intelligence runs."""

    status: RuntimeResponseStatus
    outcome: str
    reason: str


class RuntimeBridge:
    """The canonical entrypoint for interface messages.

    Use:
        bridge = RuntimeBridge(intelligence=intel_svc, services=services)
        async with db_session() as session:
            response = await bridge.process(session, request)
        # response.text → send back via the interface adapter
    """

    def __init__(
        self,
        intelligence: IntelligenceService,
        services: RuntimeServices | None = None,
        max_response_chars: int = 3500,
    ) -> None:
        self._intelligence = intelligence
        self._services = services or RuntimeServices.build(None)
        # An interface-agnostic ceiling; the interface owns its own wire
        # limits (WhatsApp chunking lives in the WhatsApp client, Phase W).
        self._max_response_chars = max_response_chars

    # -------------------------------------------------------------------
    # Entry point
    # -------------------------------------------------------------------

    async def process(
        self,
        session: AsyncSession,
        request: RuntimeRequest,
    ) -> RuntimeResponse:
        """Process a RuntimeRequest end-to-end.

        This is the ONE method interface adapters call. They get back a
        RuntimeResponse that they translate to their platform shape.
        """
        metrics = self._services.metrics
        metrics.message_started()
        started_perf = time.perf_counter()
        response = await self._process_inner(session, request)
        metrics.message_finished(response.status.value, request.interface_kind.value)
        metrics.observe_process_duration((time.perf_counter() - started_perf) * 1000)
        return response

    # -------------------------------------------------------------------
    # Pipeline
    # -------------------------------------------------------------------

    async def _process_inner(
        self,
        session: AsyncSession,
        request: RuntimeRequest,
    ) -> RuntimeResponse:
        # 1. Idempotency check + retry decision
        existing = await self._find_existing(session, request)
        retry_record: ProcessedMessageRecord | None = None
        if existing is not None:
            if existing.outcome in _RETRYABLE_OUTCOMES:
                retry_record = existing
                log.info(
                    "bridge.retry",
                    interface=request.interface_kind.value,
                    message_id=request.interface_message_id,
                    previous_outcome=existing.outcome,
                )
            else:
                log.info(
                    "bridge.duplicate",
                    interface=request.interface_kind.value,
                    message_id=request.interface_message_id,
                    original_execution=existing.execution_id,
                    outcome=existing.outcome,
                )
                return RuntimeResponse(
                    status=RuntimeResponseStatus.DUPLICATE,
                    execution_id=existing.execution_id,
                    objective_id=existing.objective_id,
                    principal_id=existing.principal_id,
                    text=existing.response_text,
                    processed_at=datetime.now(UTC),
                    duplicate_of_execution_id=existing.execution_id,
                )

        # 2. Identity resolution
        principal, _credential = await self._resolve_or_create_principal(session, request)
        if principal is None:
            return self._failure(
                RuntimeResponseStatus.PRINCIPAL_UNAUTHORIZED,
                "Could not resolve or create principal",
            )

        # 3. Security gate — the runtime enforces, never the model
        sanitizer_result: SanitizerResult | None = None
        if retry_record is None:
            rejection, sanitizer_result = await self._security_gate(session, principal, request)
            if rejection is not None:
                return await self._reject(session, request, principal, rejection)
        else:
            # Retries must not bypass the rate limiter (they cost LLM money).
            if self._services.rate_limiter.check(principal.id) is (RateLimitDecision.DENIED):
                return self._failure(
                    RuntimeResponseStatus.RATE_LIMITED,
                    "Rate limit exceeded during retry",
                )

        # 4. Idempotency record: create fresh, or adopt the failed record
        if retry_record is None:
            record = ProcessedMessageRecord(
                id=str(ULID()),
                interface_kind=request.interface_kind.value,
                interface_message_id=request.interface_message_id,
                principal_id=principal.id,
                outcome="pending",
                request_text=request.effective_text[:2000],
                received_at=request.received_at,
            )
            session.add(record)
        else:
            record = retry_record
            record.outcome = "pending"
            record.response_text = None
            record.processed_at = None
        await session.flush()  # acquire the unique constraint

        # 5. Objective + Execution — the durable "work accepted" checkpoint
        objective_repo = ObjectiveRepository(session)
        objective = await objective_repo.create(
            ObjectiveCreate(
                principal_id=principal.id,
                description=request.effective_text or "[empty message]",
                kind=ObjectiveKind.SINGLE_TURN,
                context={
                    "interface": request.interface_kind.value,
                    "interface_message_id": request.interface_message_id,
                },
            )
        )
        record.objective_id = objective.id

        exec_repo = ExecutionRepository(session)
        execution = await exec_repo.create(
            principal_id=principal.id,
            kind=ExecutionKind.SINGLE_TURN,
            objective=request.effective_text or "[empty message]",
        )
        await exec_repo.start(execution.id)
        await objective_repo.attach_execution(objective.id, execution.id)
        await objective_repo.transition(objective.id, ObjectiveStatus.IN_PROGRESS)
        record.execution_id = execution.id

        # Capture plain-string IDs BEFORE any risk of session rollback —
        # after a rollback these ORM attributes are expired and accessing
        # them would trigger synchronous IO (MissingGreenlet).
        principal_id = principal.id
        principal_display = principal.display_name
        execution_id = execution.id
        objective_id = objective.id

        await record_audit_event(
            session,
            actor_principal_id=principal.id,
            actor_kind="system",
            event_kind="bridge.execution.accepted",
            outcome="success",
            payload={
                "execution_id": execution.id,
                "objective_id": objective.id,
                "interface": request.interface_kind.value,
                "attempt": self._attempts_of(record),
            },
            request_id=execution.id,
        )
        # Commit BEFORE any intelligence runs: identity, dedup lock, objective
        # and execution are now durable. A crash here leaves honest state that
        # the recovery scan can reconcile and Meta redelivery can retry.
        await session.commit()

        # Announce the accepted interaction on the runtime event ledger:
        # "this human's message was accepted by the runtime" — emitted
        # BEFORE intelligence runs, so work scheduled WHILE processing this
        # message (watermark = now) can only be woken by the user's NEXT
        # message, never by the one being processed. Security-gate
        # rejections never reach this point (a refused prober is not an
        # interaction); later processing failures leave the fact standing
        # (the human did interact).
        from wax.runtime.work.signals import SignalRepository

        await SignalRepository(session).emit(
            f"interface.message:{principal_id}",
            payload={
                "interface": request.interface_kind.value,
                "message_id": request.interface_message_id,
            },
            emitted_by="bridge",
        )
        await session.commit()

        # 6. Context assembly via ContinuityService (single composer).
        # The current message drives relevance retrieval: the AI sees what
        # is RELEVANT, not merely what is recent.
        continuity = ContinuityService(session)
        context, conversation_id = await continuity.build_context(
            principal.id, request.interface_kind.value, request.effective_text
        )
        conversations = ConversationService(session)
        if conversation_id is None:
            conversation = await conversations.open_or_resume(
                principal.id, request.interface_kind.value
            )
            conversation_id = conversation.id

        # 7. Budget allocation for this execution
        self._services.resource_accountant.allocate(execution.id, **_DEFAULT_EXECUTION_BUDGET)

        # 8. Intelligence + persistence — the transactional unit of work
        try:
            response_text = await self._run_intelligence(
                session,
                execution_id,
                principal_id,
                principal_display,
                request,
                context,
                sanitizer_result,
            )

            # 9. Memory of the exchange
            memory_repo = MemoryRepository(session)
            await memory_repo.create(
                MemoryCreate(
                    principal_id=principal.id,
                    kind=MemoryKind.EPISODIC,
                    content={
                        "user_message": request.effective_text[:1000],
                        "assistant_response": response_text[:1000],
                        "interface": request.interface_kind.value,
                    },
                    provenance="user_statement",
                    summary=request.effective_text[:200],
                )
            )

            # 10. Complete the lifecycle honestly
            await exec_repo.complete(
                execution.id,
                checkpoint={"response": response_text[:1000]},
            )
            await objective_repo.transition(objective.id, ObjectiveStatus.SUCCEEDED)
            await conversations.touch(conversation_id, execution_id=execution.id)

            record.outcome = "success"
            record.response_text = response_text
            record.processed_at = datetime.now(UTC)

            await record_audit_event(
                session,
                actor_principal_id=principal.id,
                actor_kind="system",
                event_kind="bridge.message.processed",
                outcome="success",
                payload={
                    "execution_id": execution.id,
                    "objective_id": objective.id,
                    "interface": request.interface_kind.value,
                    "attempt": self._attempts_of(record),
                },
                request_id=execution.id,
            )
            await session.commit()

            return RuntimeResponse(
                status=RuntimeResponseStatus.SUCCESS,
                text=response_text,
                execution_id=execution.id,
                objective_id=objective.id,
                principal_id=principal.id,
                processed_at=datetime.now(UTC),
            )

        except Exception as e:
            # The work-accepted checkpoint survives the rollback; finalize on
            # top of it (execution → failed, objective → failed, record →
            # failed/dead, dead letter on exhaustion).
            await session.rollback()
            return await self._finalize_failure(
                session, request, principal_id, execution_id, objective_id, e
            )

    # -------------------------------------------------------------------
    # Intelligence
    # -------------------------------------------------------------------

    async def _run_intelligence(
        self,
        session: AsyncSession,
        execution_id: str,
        principal_id: str,
        principal_display: str | None,
        request: RuntimeRequest,
        context: ContinuityContext,
        sanitizer_result: SanitizerResult | None,
    ) -> str:
        """Run the intelligence loop and return the final response text.

        The model may request capabilities via tool calls. Every request
        passes: agency gate (policy + audit) → authority check → budget
        consumption → CapabilityInvoker (the sole effect enforcement
        point). Results return to the model as tool messages; the loop is
        bounded by settings.max_tool_rounds.

        Raises on failure — the caller owns the failure semantics.
        """
        accountant = self._services.resource_accountant
        metrics = self._services.metrics
        exec_repo = ExecutionRepository(session)

        messages = self._build_messages(
            principal_display, request, context, sanitizer_result, principal_id
        )
        tools = self._capability_tools()
        max_rounds = max(0, int(self._services.settings.max_tool_rounds))

        final_text = ""
        last_content = ""
        for round_index in range(max_rounds + 1):
            if not accountant.try_consume(
                ResourceUsage(execution_id, ResourceKind.LLM_CALLS, 1.0, notes="bridge")
            ):
                raise WaxStateConflictError(f"LLM call budget exhausted (execution={execution_id})")

            started = time.perf_counter()
            llm_response = await self._intelligence.complete(
                LLMRequest(messages=messages, tools=tools, request_id=execution_id)
            )
            duration_ms = (time.perf_counter() - started) * 1000

            provider = llm_response.provider.value
            model = llm_response.model
            metrics.observe_llm_latency(duration_ms, provider=provider, model=model)
            tokens_total = int(llm_response.usage.get("tokens_total", 0))
            accountant.try_consume(
                ResourceUsage(execution_id, ResourceKind.LLM_TOKENS, float(tokens_total))
            )
            metrics.add_llm_tokens(provider=provider, model=model, tokens_total=tokens_total)
            self._services.cost_protector.check_and_record(
                principal_id, tokens=tokens_total, messages=0
            )
            last_content = llm_response.content or last_content

            if not llm_response.tool_calls:
                await exec_repo.record_step(
                    execution_id,
                    kind="llm.complete",
                    inputs={"message_count": len(messages), "round": round_index},
                    outputs={
                        "chars": len(llm_response.content),
                        "finish_reason": llm_response.finish_reason,
                        "tokens_total": tokens_total,
                        "latency_ms": round(duration_ms, 2),
                    },
                )
                final_text = llm_response.content
                break

            if round_index == max_rounds:
                # The last allowed LLM call requested MORE tools. The round
                # budget is spent: do not execute, and be honest about it.
                break

            # Replay the assistant's tool-call turn verbatim for the provider.
            messages.append(
                LLMMessage(
                    role=MessageRole.ASSISTANT,
                    content=llm_response.content,
                    tool_calls=llm_response.tool_calls,
                )
            )

            for call in llm_response.tool_calls:
                result = await self._invoke_capability_with_gates(
                    session,
                    execution_id=execution_id,
                    principal_id=principal_id,
                    call=call,
                )
                result_payload: dict[str, Any] = {"outcome": result.outcome}
                if result.outputs is not None:
                    result_payload["outputs"] = result.outputs
                if result.error:
                    result_payload["error"] = result.error
                messages.append(
                    LLMMessage(
                        role=MessageRole.TOOL,
                        content=json.dumps(result_payload, default=str)[:8000],
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )

        if not final_text:
            # Loop exhausted while the model kept requesting tools. Be honest
            # about the boundary instead of fabricating an answer.
            final_text = last_content or (
                "I could not complete this objective within the allowed number of tool rounds."
            )

        # Truncate for interface limits
        return final_text[: self._max_response_chars]

    def _capability_tools(self) -> list[ToolSpec] | None:
        """Capability discovery: offer the registry's AVAILABLE capabilities
        to the model as tools. The model sees names/descriptions/schemas —
        never implementations, and never a guarantee of execution."""
        from wax.capabilities.contracts import CapabilityStatus

        specs: list[ToolSpec] = []
        for descriptor in self._services.capability_registry.list_capabilities():
            if (
                self._services.capability_registry.get_status(descriptor.name)
                is not CapabilityStatus.AVAILABLE
            ):
                continue
            note = " (destructive - requires human approval)" if descriptor.is_destructive else ""
            specs.append(
                ToolSpec(
                    name=descriptor.name,
                    description=f"{descriptor.description}{note}",
                    parameters=descriptor.input_schema,
                )
            )
        return specs or None

    async def _invoke_capability_with_gates(
        self,
        session: AsyncSession,
        *,
        execution_id: str,
        principal_id: str,
        call: ToolCall,
    ) -> CapabilityInvocationResult:
        """Agency gate → budget → authority → invoker, with an execution
        step recorded for every attempt. Never raises: failures come back
        as structured results the model can reason about."""
        from datetime import datetime

        from wax.agency.contracts import AgencyDecision, AgencyDecisionKind
        from wax.capabilities.contracts import (
            CapabilityInvocationRequest,
            CapabilityInvocationResult,
            CapabilityStatus,
        )
        from wax.execution.contracts import StepStatus

        exec_repo = ExecutionRepository(session)

        def _structured_failure(outcome: str, error: str) -> CapabilityInvocationResult:
            now = datetime.now(UTC)
            return CapabilityInvocationResult(
                capability_name=call.name,
                outcome=outcome,
                error=error,
                execution_id=execution_id,
                started_at=now,
                ended_at=now,
                duration_ms=0.0,
            )

        async def _finalize(
            result: CapabilityInvocationResult,
        ) -> CapabilityInvocationResult:
            await exec_repo.record_step(
                execution_id,
                kind="capability.invoke",
                inputs=dict(call.arguments),
                outputs=result.outputs,
                status=(StepStatus.SUCCEEDED if result.outcome == "success" else StepStatus.FAILED),
                capability_name=call.name,
                error=result.error,
            )
            self._services.metrics.capability_invoked(result.outcome, call.name)
            return result

        # 1. Existence + status
        try:
            descriptor, _impl = self._services.capability_registry.get(call.name)
        except Exception:
            return await _finalize(
                _structured_failure("not_found", f"No such capability: {call.name}")
            )
        if (
            self._services.capability_registry.get_status(call.name)
            is not CapabilityStatus.AVAILABLE
        ):
            return await _finalize(
                _structured_failure(
                    "denied",
                    f"Capability {call.name} is not currently available",
                )
            )

        # 2. Agency gate - the runtime decides, the AI requests (INV-04)
        agency = self._services.agency(session)
        decision = AgencyDecision(
            principal_id=principal_id,
            kind=(
                AgencyDecisionKind.DESTRUCTIVE_ACTION
                if descriptor.is_destructive
                else AgencyDecisionKind.INVOKE_CAPABILITY
            ),
            description=f"Invoke capability {call.name}",
            capability_name=call.name,
            inputs_summary={k: str(v)[:120] for k, v in list(call.arguments.items())[:5]},
        )
        verdict = await agency.evaluate(decision)
        if not verdict.approved or verdict.requires_human_approval:
            return await _finalize(
                _structured_failure(
                    "denied",
                    f"Runtime gate: {verdict.reason}. No human-approval "
                    "workflow exists yet, so this action cannot proceed.",
                )
            )

        # 3. Budget
        if not self._services.resource_accountant.try_consume(
            ResourceUsage(
                execution_id,
                ResourceKind.CAPABILITY_INVOCATIONS,
                1.0,
                notes=call.name,
            )
        ):
            return await _finalize(
                _structured_failure(
                    "denied",
                    "Resource budget exhausted for capability invocations",
                )
            )

        # 4. Authority + execution via the invoker (the SOLE effect point)
        invoker = self._services.invoker(session)
        result = await invoker.invoke(
            CapabilityInvocationRequest(
                capability_name=call.name,
                principal_id=principal_id,
                inputs=call.arguments,
                request_id=execution_id,
            )
        )
        return await _finalize(result)

    def _build_messages(
        self,
        principal_display: str | None,
        request: RuntimeRequest,
        context: ContinuityContext,
        sanitizer_result: SanitizerResult | None,
        principal_id: str | None = None,
    ) -> list[LLMMessage]:
        """Assemble the LLM message list.

        Evidence assembly is a runtime mechanism (ADR-0012): the bridge
        delivers the continuity context's evidence — objective, conver-
        sation state, ranked memories — as labelled system evidence lines
        within the configured budget. The runtime owns WHAT evidence is
        delivered; the model interprets it.
        """
        from wax.continuity.assembly import assemble_evidence, build_evidence_sections

        system_prompt = self._build_system_prompt(
            principal_display=principal_display, principal_id=principal_id
        )
        messages: list[LLMMessage] = [
            LLMMessage(role=MessageRole.SYSTEM, content=system_prompt),
        ]

        evidence_lines = assemble_evidence(
            build_evidence_sections(context),
            budget_chars=int(self._services.settings.context_char_budget),
        )
        for line in evidence_lines:
            messages.append(LLMMessage(role=MessageRole.SYSTEM, content=line))

        user_text = request.effective_text
        if sanitizer_result is not None and sanitizer_result.risk != InjectionRisk.NONE:
            # Trust-boundary marker: untrusted content is wrapped so the
            # model can treat it as data, not as instructions.
            user_text = sanitizer_result.marked_content

        messages.append(LLMMessage(role=MessageRole.USER, content=user_text))
        return messages

    # -------------------------------------------------------------------
    # Security gate
    # -------------------------------------------------------------------

    async def _security_gate(
        self,
        session: AsyncSession,
        principal: Principal,
        request: RuntimeRequest,
    ) -> tuple[_Rejection | None, SanitizerResult | None]:
        """Rate limit → cost cap → abuse/injection verdicts.

        Returns (rejection, sanitizer_result). rejection is None when the
        message may proceed.
        """
        if self._services.rate_limiter.check(principal.id) is RateLimitDecision.DENIED:
            return (
                _Rejection(
                    RuntimeResponseStatus.RATE_LIMITED,
                    "rate_limited",
                    "Rate limit exceeded",
                ),
                None,
            )

        if not self._services.cost_protector.check_and_record(principal.id):
            return (
                _Rejection(
                    RuntimeResponseStatus.RATE_LIMITED,
                    "cost_capped",
                    "Daily cost cap reached",
                ),
                None,
            )

        sanitizer_result = self._services.input_sanitizer.sanitize(
            request.effective_text or "",
            source=f"interface:{request.interface_kind.value}",
        )
        verdict = self._services.abuse_detector.evaluate(
            principal_id=principal.id,
            sender_interface_id=request.sender_interface_id,
            sanitizer_result=sanitizer_result,
        )
        # Explicit verdict fields read better than a combined boolean here.
        if verdict.recommended_action == "block" or verdict.level == AbuseLevel.HIGH:
            return (
                _Rejection(
                    RuntimeResponseStatus.PRINCIPAL_UNAUTHORIZED,
                    "blocked",
                    f"Abuse detection: {', '.join(verdict.reasons) or 'suspicious pattern'}",
                ),
                sanitizer_result,
            )
        return None, sanitizer_result

    async def _reject(
        self,
        session: AsyncSession,
        request: RuntimeRequest,
        principal: Principal,
        rejection: _Rejection,
    ) -> RuntimeResponse:
        """Persist a terminal rejection (dedup-lock it), audit + metric it."""
        record = ProcessedMessageRecord(
            id=str(ULID()),
            interface_kind=request.interface_kind.value,
            interface_message_id=request.interface_message_id,
            principal_id=principal.id,
            outcome=rejection.outcome,
            request_text=request.effective_text[:2000],
            received_at=request.received_at,
            processed_at=datetime.now(UTC),
            metadata_json={"reason": rejection.reason},
        )
        session.add(record)
        await record_audit_event(
            session,
            actor_principal_id=principal.id,
            actor_kind="system",
            event_kind="bridge.message.rejected",
            outcome=rejection.outcome,
            payload={
                "interface": request.interface_kind.value,
                "message_id": request.interface_message_id,
                "reason": rejection.reason,
            },
        )
        await session.commit()
        self._services.metrics.rate_limited(rejection.outcome)
        log.warning(
            "bridge.rejected",
            interface=request.interface_kind.value,
            message_id=request.interface_message_id,
            outcome=rejection.outcome,
            reason=rejection.reason,
        )
        return self._failure(rejection.status, rejection.reason)

    # -------------------------------------------------------------------
    # Failure finalization
    # -------------------------------------------------------------------

    async def _finalize_failure(
        self,
        session: AsyncSession,
        request: RuntimeRequest,
        principal_id: str | None,
        execution_id: str | None,
        objective_id: str | None,
        error: Exception,
    ) -> RuntimeResponse:
        """Honest failure semantics: mark execution + objective failed,
        make the idempotency record retryable (or dead after N attempts),
        and write a dead letter when the work is terminal.
        """
        final_outcome = "failed"
        attempts = 1
        try:
            record = await self._find_existing(session, request)
            if record is not None:
                meta = dict(record.metadata_json or {})
                attempts = int(meta.get("attempts", 1)) + 1
                meta["attempts"] = attempts
                meta["last_error_type"] = type(error).__name__
                meta["last_error"] = str(error)[:500]
                record.metadata_json = meta
                final_outcome = "dead" if attempts >= MAX_ATTEMPTS_PER_MESSAGE else "failed"
                record.outcome = final_outcome
                record.processed_at = datetime.now(UTC)

            exec_repo = ExecutionRepository(session)
            if execution_id is not None:
                await exec_repo.fail(execution_id, f"{type(error).__name__}: {error}"[:1000])
            if objective_id is not None:
                await ObjectiveRepository(session).transition(objective_id, ObjectiveStatus.FAILED)

            if final_outcome == "dead":
                await DeadLetterRepository(session).record(
                    kind="bridge.message",
                    principal_id=principal_id,
                    execution_id=execution_id,
                    error_type=type(error).__name__,
                    error_message=str(error)[:5000],
                    attempts=attempts,
                    payload={
                        "interface": request.interface_kind.value,
                        "message_id": request.interface_message_id,
                        "request_text": (record.request_text if record else "")[:500],
                    },
                )

            await record_audit_event(
                session,
                actor_principal_id=principal_id,
                actor_kind="system",
                event_kind="bridge.message.failed",
                outcome=final_outcome,
                payload={
                    "execution_id": execution_id,
                    "objective_id": objective_id,
                    "interface": request.interface_kind.value,
                    "message_id": request.interface_message_id,
                    "attempts": attempts,
                    "error_type": type(error).__name__,
                },
                request_id=execution_id,
            )
            await session.commit()
        except Exception as finalize_error:
            # The original work-accepted rows stay pending/running; the
            # recovery scan reconciles them and Meta redelivery retries.
            log.critical(
                "bridge.finalize_failure_failed",
                message_id=request.interface_message_id,
                original_error=str(error),
                finalize_error=str(finalize_error),
            )

        self._services.metrics.llm_error("bridge")
        log.error(
            "bridge.process.error",
            interface=request.interface_kind.value,
            message_id=request.interface_message_id,
            error=str(error),
            error_type=type(error).__name__,
            outcome=final_outcome,
            attempts=attempts,
        )
        return self._failure(
            RuntimeResponseStatus.INTERNAL_ERROR,
            f"Internal error: {type(error).__name__}",
        )

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _attempts_of(record: ProcessedMessageRecord) -> int:
        meta = record.metadata_json or {}
        return int(meta.get("attempts", 1))

    async def _find_existing(
        self, session: AsyncSession, request: RuntimeRequest
    ) -> ProcessedMessageRecord | None:
        """Look up an already-processed message by (interface_kind, message_id)."""
        result = await session.execute(
            select(ProcessedMessageRecord).where(
                ProcessedMessageRecord.interface_kind == request.interface_kind.value,
                ProcessedMessageRecord.interface_message_id == request.interface_message_id,
            )
        )
        return result.scalar_one_or_none()

    async def _resolve_or_create_principal(
        self, session: AsyncSession, request: RuntimeRequest
    ) -> tuple[Principal | None, Any | None]:
        """Resolve sender_interface_id → Principal. Create on first contact.

        First contact also assigns the default role (member) so the
        authorization system has something true to enforce, and marks the
        credential used on every return visit.
        """
        credential_kind = _INTERFACE_CREDENTIAL_KIND.get(request.interface_kind)
        if credential_kind is None:
            log.error(
                "bridge.unknown_interface",
                interface=request.interface_kind.value,
            )
            return None, None
        if credential_kind not in ALLOWED_CREDENTIAL_KINDS:
            log.error(
                "bridge.unknown_credential_kind",
                kind=credential_kind,
                interface=request.interface_kind.value,
            )
            return None, None

        repo = PrincipalRepository(session)
        credential = await repo.find_credential(credential_kind, request.sender_interface_id)
        if credential is not None:
            principal = await repo.get_principal(credential.principal_id)
            if principal is not None:
                await repo.mark_credential_used(credential.id)
                return principal, credential

        # First contact — create principal + credential + default role
        principal = await repo.create_principal(display_name=request.sender_display_name)
        credential = await repo.add_credential(
            principal.id,
            kind=credential_kind,
            value=request.sender_interface_id,
            is_verified=True,  # the interface vouched for this ID
        )
        await ensure_principal_role(session, principal.id, DEFAULT_ROLE_FOR_NEW_PRINCIPALS)
        await record_audit_event(
            session,
            actor_principal_id=principal.id,
            actor_kind="system",
            event_kind="identity.principal.registered",
            outcome="success",
            payload={
                "interface": request.interface_kind.value,
                "credential_kind": credential_kind,
            },
        )
        log.info(
            "bridge.principal_created",
            principal_id=principal.id,
            interface=request.interface_kind.value,
            credential_kind=credential_kind,
        )
        return principal, credential

    def _build_system_prompt(
        self, *, principal_display: str | None, principal_id: str | None = None
    ) -> str:
        """Build the system prompt for the LLM.

        This is intentionally minimal. WAX does NOT hardcode a tutor
        persona (Directive §5.1, §10). The AI determines its own behavior
        based on the user's objective. Contextual evidence (objective,
        conversation state, memories) is NOT baked in here — it is
        delivered as separate labelled evidence lines by the assembly
        mechanism, so the runtime can budget, rank, and attribute it.

        The principal id IS an environment fact (like a process id): the
        intelligence needs it to compose runtime names such as the
        interface.message:<principal_id> wake condition.
        """
        name_str = f" The user's name is {principal_display}." if principal_display else ""
        principal_str = (
            f" The principal you act for has id {principal_id}." if principal_id else ""
        )
        return (
            "You are an AI operating inside the WAX runtime. You are intelligent; "
            "WAX is the environment that holds memory, identity, capabilities, and "
            "authorization on your behalf."
            + name_str
            + principal_str
            + " Help the user pursue their objective. Be concise and useful. If you "
            "need a capability you don't have, say so explicitly rather than "
            "fabricating."
        )

    def _failure(self, status: RuntimeResponseStatus, error: str) -> RuntimeResponse:
        return RuntimeResponse(
            status=status,
            error=error,
            processed_at=datetime.now(UTC),
        )
