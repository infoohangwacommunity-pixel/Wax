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

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.authority.seed import DEFAULT_ROLE_FOR_NEW_PRINCIPALS, ensure_principal_role
from wax.continuity.contracts import ContinuityContext
from wax.continuity.service import ContinuityService, ConversationService
from wax.core.exceptions import WaxStateConflictError
from wax.execution.contracts import ExecutionKind
from wax.execution.repository import ExecutionRepository
from wax.identity.contracts import ALLOWED_CREDENTIAL_KINDS
from wax.identity.repository import PrincipalRepository
from wax.intelligence.contracts import LLMMessage, LLMRequest, MessageRole
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
        # WhatsApp text messages are limited to 4096 chars; we cap conservatively.
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

        # 6. Context assembly via ContinuityService (single composer)
        continuity = ContinuityService(session)
        context, conversation_id = await continuity.build_context(
            principal.id, request.interface_kind.value
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
                session, execution.id, principal, request, context, sanitizer_result
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
        principal: Principal,
        request: RuntimeRequest,
        context: ContinuityContext,
        sanitizer_result: SanitizerResult | None,
    ) -> str:
        """Run the intelligence step and return the response text.

        Consumes the execution's LLM budget and records usage to the cost
        protector + metrics. Raises on failure — the caller owns the
        failure semantics.
        """
        accountant = self._services.resource_accountant
        if not accountant.try_consume(
            ResourceUsage(execution_id, ResourceKind.LLM_CALLS, 1.0, notes="bridge")
        ):
            raise WaxStateConflictError(
                f"Resource budget exhausted before LLM call (execution={execution_id})"
            )

        messages = self._build_messages(principal, request, context, sanitizer_result)

        started = time.perf_counter()
        llm_response = await self._intelligence.complete(
            LLMRequest(messages=messages, request_id=execution_id)
        )
        duration_ms = (time.perf_counter() - started) * 1000

        metrics = self._services.metrics
        provider = llm_response.provider.value
        model = llm_response.model
        metrics.observe_llm_latency(duration_ms, provider=provider, model=model)

        tokens_total = int(llm_response.usage.get("tokens_total", 0))
        accountant.try_consume(
            ResourceUsage(execution_id, ResourceKind.LLM_TOKENS, float(tokens_total))
        )
        metrics.add_llm_tokens(provider=provider, model=model, tokens_total=tokens_total)
        self._services.cost_protector.check_and_record(
            principal.id, tokens=tokens_total, messages=0
        )

        exec_repo = ExecutionRepository(session)
        await exec_repo.record_step(
            execution_id,
            kind="llm.complete",
            inputs={"message_count": len(messages)},
            outputs={
                "chars": len(llm_response.content),
                "finish_reason": llm_response.finish_reason,
                "tokens_total": tokens_total,
                "latency_ms": round(duration_ms, 2),
            },
        )

        # Truncate for interface limits
        return llm_response.content[: self._max_response_chars]

    def _build_messages(
        self,
        principal: Principal,
        request: RuntimeRequest,
        context: ContinuityContext,
        sanitizer_result: SanitizerResult | None,
    ) -> list[LLMMessage]:
        """Assemble the LLM message list from continuity context."""
        system_prompt = self._build_system_prompt(
            principal_display=principal.display_name,
            memory_count=len(context.recent_memories),
        )
        messages: list[LLMMessage] = [
            LLMMessage(role=MessageRole.SYSTEM, content=system_prompt),
        ]

        for memory in reversed(context.recent_memories):
            summary = memory.get("summary") or ""
            messages.append(
                LLMMessage(
                    role=MessageRole.SYSTEM,
                    content=f"[prior memory: {summary}]",
                )
            )

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

    def _build_system_prompt(self, *, principal_display: str | None, memory_count: int) -> str:
        """Build the system prompt for the LLM.

        This is intentionally minimal. WAX does NOT hardcode a tutor
        persona (Directive §5.1, §10). The AI determines its own behavior
        based on the user's objective.
        """
        name_str = f" The user's name is {principal_display}." if principal_display else ""
        memory_str = (
            f" You have {memory_count} recent memories about this user." if memory_count > 0 else ""
        )
        return (
            "You are an AI operating inside the WAX runtime. You are intelligent; "
            "WAX is the environment that holds memory, identity, capabilities, and "
            "authorization on your behalf."
            + name_str
            + memory_str
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
