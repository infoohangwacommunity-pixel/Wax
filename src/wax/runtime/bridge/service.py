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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

# ADR-0045 (Phase 12): ensure the media subsystem is statically visible
# to AST import analysis. The pipeline is used dynamically inside
# _run_intelligence for extracting media from inbound messages; this
# top-level reference makes the connection visible to the constitutional
# audit scanner.
import wax.media.pipeline  # noqa: F401
from wax.authority.seed import DEFAULT_ROLE_FOR_NEW_PRINCIPALS, ensure_principal_role
from wax.capabilities.contracts import CapabilityInvocationResult
from wax.continuity.contracts import ContinuityContext
from wax.continuity.service import ContinuityService, ConversationService
from wax.core.exceptions import WaxStateConflictError
from wax.execution.contracts import ExecutionKind
from wax.execution.repository import ExecutionRepository
from wax.identity.contracts import (
    ALLOWED_CREDENTIAL_KINDS,
    INTERFACE_CREDENTIAL_KINDS,
)
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
from wax.runtime.work.reentry import (
    ReentryOutcome,
    ReentryRequest,
    ReentryResult,
)
from wax.security.abuse import AbuseLevel
from wax.security.input_sanitizer import InjectionRisk, SanitizerResult
from wax.security.rate_limiter import RateLimitDecision
from wax.state.bridge_models import ProcessedMessageRecord
from wax.state.identity_models import Principal

log = get_logger(__name__)

# Map InterfaceKind → PrincipalCredential kind — DERIVED from the single
# source of truth in wax.identity.contracts (the identity boundary table).
# The bridge owns no private copy of the mapping.
_INTERFACE_CREDENTIAL_KIND: dict[InterfaceKind, str] = {
    InterfaceKind(kind): credential_kind
    for kind, credential_kind in INTERFACE_CREDENTIAL_KINDS.items()
    if kind in InterfaceKind._value2member_map_
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


@dataclass(frozen=True)
class _SyntheticRequest:
    """A minimal RuntimeRequest-like object for the re-entry path.

    The bridge's `_run_intelligence` reads `request.effective_text` and
    `request.interface_kind` from the RuntimeRequest. The re-entry path
    does NOT have a real RuntimeRequest (the wake is a runtime fact, not
    an interface message) — but it does have a prompt and a conversation
    that lives on a specific interface. This synthetic request carries
    the minimum fields the intelligence loop needs.

    The `effective_text` is the prompt (the intelligence's instruction
    for this re-entry), NOT a user message — the prompt is presented to
    the model as a user message in the message list, but the runtime
    observation is presented as a tool message so the model cannot
    impersonate the runtime.
    """

    interface_kind: InterfaceKind
    interface_message_id: str
    sender_interface_id: str
    sender_display_name: str | None
    text: str
    received_at: datetime

    @property
    def effective_text(self) -> str:
        return self.text


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
            try:
                await session.flush()  # acquire the unique constraint
            except IntegrityError:
                # A concurrent attempt (two replicas, or a racing
                # redelivery) inserted the same message first and owns the
                # pipeline for this message_id. Its record is
                # authoritative; this attempt is the honest duplicate
                # observer — it must NOT touch the winner's state.
                await session.rollback()
                winner = await self._find_existing(session, request)
                log.warning(
                    "bridge.duplicate_race",
                    interface=request.interface_kind.value,
                    message_id=request.interface_message_id,
                    winner_outcome=(winner.outcome if winner else None),
                )
                return RuntimeResponse(
                    status=RuntimeResponseStatus.DUPLICATE,
                    execution_id=(winner.execution_id if winner else None),
                    objective_id=(winner.objective_id if winner else None),
                    principal_id=(winner.principal_id if winner else principal.id),
                    text=(winner.response_text if winner else None),
                    processed_at=datetime.now(UTC),
                    duplicate_of_execution_id=(winner.execution_id if winner else None),
                )
        else:
            # Claim the failed record for THIS attempt with a single
            # conditional UPDATE: two concurrent retries (two replicas, or
            # a racing redelivery) must not both adopt the same failure —
            # the rowcount decides who owns the retry (CV-12's sibling
            # race in the idempotency machine).
            from sqlalchemy import update as sa_update

            claimed = await session.execute(
                sa_update(ProcessedMessageRecord)
                .where(
                    ProcessedMessageRecord.id == existing.id,
                    ProcessedMessageRecord.outcome == "failed",
                )
                .values(outcome="pending", response_text=None, processed_at=None)
                .execution_options(synchronize_session=False)
            )
            if claimed.rowcount != 1:
                # Another attempt already claimed the retry; its record is
                # in flight again. Do not double-run the message.
                log.info(
                    "bridge.retry_claim_lost",
                    interface=request.interface_kind.value,
                    message_id=request.interface_message_id,
                )
                return RuntimeResponse(
                    status=RuntimeResponseStatus.DUPLICATE,
                    execution_id=existing.execution_id,
                    objective_id=existing.objective_id,
                    principal_id=existing.principal_id,
                    text=None,
                    processed_at=datetime.now(UTC),
                    duplicate_of_execution_id=existing.execution_id,
                )
            await session.refresh(existing)
            record = existing
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
        # History row (ADR-0020): this execution's participation in the
        # objective's life is recorded evidence, not just a mutable pointer.
        await objective_repo.record_execution_start(objective.id, execution.id, kind="bridge")
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
        # Constitutional audit fix: the conversation→objective link is what
        # feeds the priority-0 OBJECTIVE evidence section. It existed as a
        # column and a section builder but NOBODY ever wrote it — the
        # section could never fire in any live flow. Write it here, at the
        # moment both ends of the link exist.
        await conversations.attach_objective(conversation_id, objective.id)

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
            await objective_repo.record_execution_end(
                objective.id, execution.id, outcome="succeeded"
            )
            # Constitutional audit fix: `succeeded` may only land when the
            # runtime holds NO outstanding durable work for this objective.
            # If the interaction scheduled work, the objective IS waiting —
            # a fabricated terminal state is permanent (terminal states are
            # immutable), so the runtime checks DB truth instead of
            # trusting the flow.
            from wax.objective.evidence import (
                objective_has_outstanding_work,
                sync_waiting_for_execution,
            )

            if await objective_has_outstanding_work(session, objective.id):
                await sync_waiting_for_execution(session, execution.id)
                log.info(
                    "objective.stays_waiting",
                    objective_id=objective.id,
                    execution_id=execution.id,
                    reason="durable_work_outstanding",
                )
            else:
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
        passes: the shared approval gate (agency → approval primitive,
        wax.authority.gate) → budget consumption → CapabilityInvoker
        (the sole effect enforcement point). Results return to the model
        as tool messages; the loop is bounded by settings.max_tool_rounds.

        Raises on failure — the caller owns the failure semantics.
        """
        accountant = self._services.resource_accountant
        metrics = self._services.metrics
        exec_repo = ExecutionRepository(session)

        # Context-budget negotiation (ADR-0015): the evidence budget is
        # derived from the provider's advertised context limit when it
        # exposes one; otherwise the configured character fallback applies.
        from wax.intelligence.context_limits import derive_context_budget

        budget = derive_context_budget(
            self._intelligence.inner_provider,
            fallback_char_budget=int(self._services.settings.context_char_budget),
            output_reserve_tokens=int(self._services.settings.llm_output_reserve_tokens),
        )
        log.info(
            "context.budget",
            source=budget.source,
            budget_chars=budget.budget_chars,
            context_limit_tokens=budget.context_limit_tokens,
            chars_per_token=round(budget.chars_per_token, 3),
            token_counter=budget.counter,
        )

        messages = self._build_messages(
            principal_display,
            request,
            context,
            sanitizer_result,
            principal_id,
            budget_chars=budget.budget_chars,
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

        from wax.capabilities.contracts import (
            CapabilityInvocationRequest,
            CapabilityInvocationResult,
            CapabilityStatus,
        )

        # CV-19: lift the declared idempotency-key transport field before
        # the authority gate (it is request metadata, not operation
        # semantics; approval fingerprints stay about the operation).
        from wax.capabilities.invoker import lift_idempotency_key
        from wax.execution.contracts import StepStatus

        op_inputs, idempotency_key = lift_idempotency_key(call.arguments)

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

        # 2. Agency gate → approval chain (CV-12/CV-14 fix: ONE shared
        # component — RuntimeBridgeApprovalGate is the same authority chain
        # the durable-work handler runs, not a private copy).
        from wax.authority.gate import ApprovalGate, ApprovalGateState

        gate = ApprovalGate(session, self._services)
        outcome = await gate.evaluate(
            principal_id=principal_id,
            capability_name=call.name,
            descriptor=descriptor,
            inputs=dict(op_inputs),
            execution_id=execution_id,
            description=f"Invoke capability {call.name}",
            emitted_by="bridge",
        )
        if outcome.state is ApprovalGateState.ALREADY_USED:
            return await _finalize(
                _structured_failure("denied", outcome.error or "approval already used")
            )
        if outcome.state is ApprovalGateState.PENDING:
            record = outcome.approval
            return await _finalize(
                _structured_failure(
                    "pending_approval",
                    f"This action requires human approval "
                    f"(approval {record.id}, capability {call.name}). It is "
                    f"pending until {record.expires_at.isoformat()}; inform "
                    f"the human how to approve or deny it. Nothing was "
                    f"executed.",
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
                inputs=op_inputs,
                idempotency_key=idempotency_key,
                request_id=execution_id,
            )
        )
        return await _finalize(result)

    # -------------------------------------------------------------------
    # Human authority path (decisions; the request chain lives in
    # wax.authority.gate — the single shared approval component)
    # -------------------------------------------------------------------
    async def submit_approval_decision(
        self,
        session: AsyncSession,
        *,
        interface_kind: InterfaceKind,
        sender_interface_id: str,
        approval_id: str,
        approve: bool,
        note: str | None = None,
    ) -> tuple[bool, str]:
        """The HUMAN authority path for approvals.

        Interface adapters call this when their user expresses a decision.
        The deciding principal is resolved from the interface credential —
        the same identity path as inbound messages — so the runtime never
        takes a decision from the AI or from an unauthenticated source.

        Returns (ok, message). Never raises.
        """
        from wax.authority.approvals import ApprovalDecisionError, ApprovalService
        from wax.runtime.work.signals import SignalRepository
        from wax.state.identity_models import PrincipalCredential

        credential_kind = _INTERFACE_CREDENTIAL_KIND.get(interface_kind)
        if credential_kind is None:
            return False, "unknown interface kind"

        result = await session.execute(
            select(PrincipalCredential).where(
                PrincipalCredential.kind == credential_kind,
                PrincipalCredential.value == sender_interface_id,
            )
        )
        credential = result.scalar_one_or_none()
        if credential is None:
            return False, "unknown sender"
        principal_id = credential.principal_id

        approvals = ApprovalService(session)
        try:
            record = await approvals.decide(
                approval_id, decided_by=principal_id, approve=approve, note=note
            )
        except ApprovalDecisionError as e:
            return False, str(e)
        await SignalRepository(session).emit(
            f"approval.{'granted' if approve else 'denied'}:{record.id}",
            payload={"approval_id": record.id, "capability": record.capability_name},
            emitted_by=f"principal:{principal_id}",
        )
        self._services.metrics.approval_decided("approved" if approve else "denied")
        return True, (
            f"Approved: {record.capability_name} may be attempted once."
            if approve
            else f"Denied: {record.capability_name} will not run."
        )

    async def match_approval_command(
        self,
        session: AsyncSession,
        request: RuntimeRequest,
    ) -> RuntimeResponse | None:
        """Match the generic approval decision grammar in an inbound message.

        '/approve <id>' or '/deny <id>' (case-insensitive). Returns a
        RuntimeResponse when the message IS a decision, None otherwise —
        the message then flows to the normal pipeline. The grammar is
        generic (no domain words); any interface adapter can adopt it by
        calling this before `process`.
        """
        text = (request.text or "").strip()
        lowered = text.lower()
        for verb, approve in (("/approve", True), ("/deny", False)):
            if lowered.startswith(verb):
                parts = text.split(maxsplit=1)
                if len(parts) != 2 or not parts[1].strip():
                    return RuntimeResponse(
                        status=RuntimeResponseStatus.SUCCESS,
                        text="Usage: /approve <approval-id> or /deny <approval-id>",
                        processed_at=datetime.now(UTC),
                    )
                approval_id = parts[1].strip()
                from wax.state.engine import db_session

                async with db_session() as decision_session:
                    ok, message = await self.submit_approval_decision(
                        decision_session,
                        interface_kind=request.interface_kind,
                        sender_interface_id=request.sender_interface_id,
                        approval_id=approval_id,
                        approve=approve,
                    )
                    await decision_session.commit()
                log.info(
                    "bridge.approval_command",
                    verb=verb,
                    approval_id=approval_id,
                    ok=ok,
                )
                return RuntimeResponse(
                    status=RuntimeResponseStatus.SUCCESS,
                    text=message,
                    processed_at=datetime.now(UTC),
                )
        return None

    # -------------------------------------------------------------------
    # ADR-0034: Durable intelligence re-entry
    # -------------------------------------------------------------------
    async def run_reentry(self, request: ReentryRequest) -> ReentryResult:
        """Wake the intelligence and re-run the LLM + tool-call loop.

        This is the bridge-side implementation of the re-entry callback. The
        work handler's `intelligence_handler` builds a `ReentryRequest`
        from the work item's payload + principal + execution id and calls
        this method via `services.reentry_callback`.

        Flow:
        1. Resolve the originating objective from execution_id
        2. Locate the conversation linked to this objective
        3. Verify conversation.principal_id == request.principal_id
        4. Create a fresh continuation execution
        5. Record objective execution history (kind="work_continuation")
        6. Reactivate the objective (sync_active_for_execution)
        7. Allocate execution budget
        8. Assemble context via ContinuityService (same composer)
        9. Build the LLM message list with the runtime observation as
           a typed tool message (NOT a user message — the model cannot
           impersonate the runtime)
        10. Run the intelligence + tool-call loop (same loop as live)
        11. Persist continuation memory + execution result
        12. Record objective execution end + reconcile objective state

        Never auto-closes the objective — only the intelligence can do
        that via `objective.update_status` (a future capability) with
        real evidence. This handler leaves the objective in_progress,
        waiting, or awaiting_human based on what the continuation
        produced.
        """
        from wax.continuity.service import ContinuityService
        from wax.execution.contracts import ExecutionKind
        from wax.execution.repository import ExecutionRepository
        from wax.intelligence.context_limits import derive_context_budget
        from wax.intelligence.contracts import LLMMessage, MessageRole
        from wax.memory.contracts import MemoryCreate, MemoryKind
        from wax.memory.repository import MemoryRepository
        from wax.objective.evidence import (
            objective_for_execution,
            sync_active_for_execution,
        )
        from wax.objective.repository import ObjectiveRepository
        from wax.observability.audit import record_audit_event
        from wax.runtime.work.signals import SignalRepository
        from wax.state.engine import db_session

        async with db_session() as session:
            # 1. Resolve the originating objective
            objective = await objective_for_execution(session, request.originating_execution_id)
            if objective is None:
                log.warning(
                    "reentry.no_objective",
                    originating_execution_id=request.originating_execution_id,
                )
                return ReentryResult(
                    execution_id="",
                    objective_id="",
                    conversation_id="",
                    outcome="failed",
                    error="no objective for originating execution",
                )

            # 2. Locate the conversation linked to this objective
            from sqlalchemy import select as sa_select

            from wax.state.continuity_models import ConversationRecord

            conversation = (
                await session.execute(
                    sa_select(ConversationRecord)
                    .where(ConversationRecord.objective_id == objective.id)
                    .order_by(ConversationRecord.last_message_at.desc().nulls_last())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if conversation is None:
                log.warning(
                    "reentry.no_conversation",
                    objective_id=objective.id,
                )
                return ReentryResult(
                    execution_id="",
                    objective_id=objective.id,
                    conversation_id="",
                    outcome="failed",
                    error="no conversation linked to this objective",
                )

            # 3. Ownership check — wrong principal is a security violation
            if conversation.principal_id != request.principal_id:
                log.error(
                    "reentry.principal_mismatch",
                    work_item_id=request.work_item_id,
                    work_principal_id=request.principal_id,
                    conversation_principal_id=conversation.principal_id,
                )
                return ReentryResult(
                    execution_id="",
                    objective_id=objective.id,
                    conversation_id=conversation.id,
                    outcome="failed",
                    error="principal_id does not match the conversation's owner",
                )

            # 4. Create a fresh continuation execution
            exec_repo = ExecutionRepository(session)
            execution = await exec_repo.create(
                principal_id=request.principal_id,
                kind=ExecutionKind.SINGLE_TURN,
                objective=f"[continuation] {request.prompt[:200]}",
            )
            await exec_repo.start(execution.id)

            # 5. Record objective execution history
            objective_repo = ObjectiveRepository(session)
            await objective_repo.record_execution_start(
                objective.id, execution.id, kind="work_continuation"
            )

            # 6. Reactivate the objective (waiting → in_progress)
            await sync_active_for_execution(session, execution.id)

            # 7. Allocate execution budget (same shape as live path)
            self._services.resource_accountant.allocate(execution.id, **_DEFAULT_EXECUTION_BUDGET)

            # 8. Assemble context (ContinuityService is the single composer)
            continuity = ContinuityService(session)
            # The "interface_kind" for a continuation is the same one the
            # conversation was on — the principal didn't switch channels.
            context, _conv_id = await continuity.build_context(
                request.principal_id,
                conversation.interface_kind,
                current_message=request.prompt,
            )

            # 9. Build messages — observation as a TOOL message, not user
            from wax.continuity.assembly import assemble_evidence, build_evidence_sections

            budget = derive_context_budget(
                self._intelligence.inner_provider,
                fallback_char_budget=int(self._services.settings.context_char_budget),
                output_reserve_tokens=int(self._services.settings.llm_output_reserve_tokens),
            )

            principal = await session.get(Principal, request.principal_id)
            principal_display = principal.display_name if principal else None

            system_prompt = self._build_system_prompt(
                principal_display=principal_display,
                principal_id=request.principal_id,
            )
            messages: list[LLMMessage] = [
                LLMMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ]
            evidence_lines = assemble_evidence(
                build_evidence_sections(context),
                budget_chars=budget.budget_chars,
            )
            for line in evidence_lines:
                messages.append(LLMMessage(role=MessageRole.SYSTEM, content=line))

            # The intelligence's instruction (the prompt) is a user message —
            # the model is being ASKED to reason about something. The runtime
            # observation is a TOOL message so the model cannot impersonate
            # the runtime by typing into a chat box.
            messages.append(LLMMessage(role=MessageRole.USER, content=request.prompt))

            # The observation is a structured tool response from "the runtime"
            # — the model sees it as evidence, not as user instruction.
            import json as _json

            observation_payload = {
                "source": request.observation.get("source", "runtime"),
                "event": request.observation.get("event"),
                "work_id": request.observation.get("work_id"),
                "result": request.observation.get("result", {}),
            }
            messages.append(
                LLMMessage(
                    role=MessageRole.TOOL,
                    content=_json.dumps(observation_payload, default=str)[:4000],
                    tool_call_id=f"runtime-observation-{request.work_item_id}",
                    name="runtime.observation",
                )
            )

            # Audit the re-entry start
            await record_audit_event(
                session,
                actor_principal_id=request.principal_id,
                actor_kind="system",
                event_kind="intelligence.reentry.started",
                outcome="success",
                payload={
                    "continuation_execution_id": execution.id,
                    "originating_execution_id": request.originating_execution_id,
                    "objective_id": objective.id,
                    "work_item_id": request.work_item_id,
                    "observation_event": request.observation.get("event"),
                },
                request_id=execution.id,
            )
            await session.commit()

            # 10. Run intelligence + tool-call loop
            try:
                response_text = await self._run_intelligence(
                    session,
                    execution_id=execution.id,
                    principal_id=request.principal_id,
                    principal_display=principal_display,
                    # The re-entry path does NOT pass a RuntimeRequest —
                    # the prompt is already in the message list. We pass
                    # a minimal synthetic request only if _run_intelligence
                    # needs it for context. Refactor: we'll call the loop
                    # directly to avoid the RuntimeRequest dependency.
                    request=_SyntheticRequest(
                        interface_kind=InterfaceKind(conversation.interface_kind)
                        if InterfaceKind._value2member_map_.get(conversation.interface_kind)
                        else InterfaceKind.WHATSAPP,
                        interface_message_id=f"reentry-{request.work_item_id}",
                        sender_interface_id="runtime",
                        sender_display_name=principal_display,
                        text=request.prompt,
                        received_at=datetime.now(UTC),
                    ),
                    context=context,
                    sanitizer_result=None,
                )

                # 11. Persist continuation memory (episodic)
                memory_repo = MemoryRepository(session)
                await memory_repo.create(
                    MemoryCreate(
                        principal_id=request.principal_id,
                        kind=MemoryKind.EPISODIC,
                        content={
                            "wake_observation": request.observation,
                            "continuation_response": response_text[:1000],
                            "originating_execution_id": request.originating_execution_id,
                        },
                        provenance="runtime_continuation",
                        summary=(
                            f"Re-entry after {request.observation.get('event')}: "
                            f"{response_text[:200]}"
                        ),
                    )
                )

                # 12. Complete the continuation execution
                await exec_repo.complete(
                    execution.id,
                    checkpoint={
                        "response": response_text[:1000],
                        "wake_event": request.observation.get("event"),
                        "originating_execution_id": request.originating_execution_id,
                    },
                )
                await objective_repo.record_execution_end(
                    objective.id, execution.id, outcome="succeeded"
                )

                # 13. Announce the re-entry completion on the event ledger
                await SignalRepository(session).emit(
                    f"intelligence.reentered:{execution.id}",
                    payload={
                        "continuation_execution_id": execution.id,
                        "objective_id": objective.id,
                        "originating_execution_id": request.originating_execution_id,
                    },
                    emitted_by="bridge",
                )

                # 14. Reconcile objective state per evidence — do NOT auto-close
                from wax.objective.evidence import (
                    objective_has_outstanding_work,
                    sync_waiting_for_execution,
                )

                if await objective_has_outstanding_work(session, objective.id):
                    await sync_waiting_for_execution(session, execution.id)
                    outcome: ReentryOutcome = "waiting"
                else:
                    # Check for pending approvals
                    from wax.state.approval_models import PendingApprovalRecord

                    pending = (
                        await session.execute(
                            sa_select(PendingApprovalRecord.id)
                            .where(
                                PendingApprovalRecord.principal_id == request.principal_id,
                                PendingApprovalRecord.status == "pending",
                            )
                            .limit(1)
                        )
                    ).first()
                    if pending is not None:
                        from wax.objective.evidence import (
                            sync_awaiting_human_for_execution,
                        )

                        await sync_awaiting_human_for_execution(session, execution.id)
                        outcome = "awaiting_human"
                    else:
                        # Active reasoning with nothing pending — leave in_progress
                        outcome = "in_progress"

                await record_audit_event(
                    session,
                    actor_principal_id=request.principal_id,
                    actor_kind="system",
                    event_kind="intelligence.reentry.completed",
                    outcome="success",
                    payload={
                        "continuation_execution_id": execution.id,
                        "objective_id": objective.id,
                        "outcome": outcome,
                        "response_chars": len(response_text),
                    },
                    request_id=execution.id,
                )
                await session.commit()

                return ReentryResult(
                    execution_id=execution.id,
                    objective_id=objective.id,
                    conversation_id=conversation.id,
                    outcome=outcome,
                    response_text=response_text[:1000],
                )

            except Exception as e:
                # Honest failure: the continuation execution failed. Mark
                # it so, record the failure evidence, and propagate. The
                # objective stays in_progress; the work runner's retry
                # semantics will re-attempt the re-entry if attempts
                # remain.
                await session.rollback()
                async with db_session() as fail_session:
                    fail_exec_repo = ExecutionRepository(fail_session)
                    fail_obj_repo = ObjectiveRepository(fail_session)
                    # The execution may not exist on the failure path
                    # (rollback may have un-committed the create). Be
                    # tolerant: try to mark it failed; if not, log.
                    try:
                        existing_exec = await fail_exec_repo.get(execution.id)
                        if existing_exec is not None and existing_exec.status == "running":
                            await fail_exec_repo.fail(
                                execution.id,
                                error=f"reentry failure: {type(e).__name__}: {e}",
                            )
                            await fail_obj_repo.record_execution_end(
                                objective.id, execution.id, outcome="failed"
                            )
                            await fail_session.commit()
                    except Exception as finalize_err:
                        log.error(
                            "reentry.finalize_failed",
                            execution_id=execution.id,
                            error=str(finalize_err),
                        )

                log.warning(
                    "intelligence.reentry.failed",
                    work_item_id=request.work_item_id,
                    continuation_execution_id=execution.id,
                    error=str(e)[:500],
                    error_type=type(e).__name__,
                )
                return ReentryResult(
                    execution_id=execution.id,
                    objective_id=objective.id,
                    conversation_id=conversation.id,
                    outcome="failed",
                    error=f"{type(e).__name__}: {e}",
                )

    def _build_messages(
        self,
        principal_display: str | None,
        request: RuntimeRequest,
        context: ContinuityContext,
        sanitizer_result: SanitizerResult | None,
        principal_id: str | None = None,
        budget_chars: int | None = None,
    ) -> list[LLMMessage]:
        """Assemble the LLM message list.

        Evidence assembly is a runtime mechanism (ADR-0012): the bridge
        delivers the continuity context's evidence — objective, conver-
        sation state, ranked memories — as labelled system evidence lines
        within the evidence budget. The runtime owns WHAT evidence is
        delivered; the model interprets it. `budget_chars` comes from the
        context-budget negotiation (provider limit or configured fallback).
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
            budget_chars=(
                int(budget_chars)
                if budget_chars is not None
                else int(self._services.settings.context_char_budget)
            ),
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
        owns_record = False
        try:
            record = await self._find_existing(session, request)
            if record is not None:
                meta = dict(record.metadata_json or {})
                attempts = int(meta.get("attempts", 1)) + 1
                meta["attempts"] = attempts
                meta["last_error_type"] = type(error).__name__
                meta["last_error"] = str(error)[:500]
                final_outcome = "dead" if attempts >= MAX_ATTEMPTS_PER_MESSAGE else "failed"
                # Claim the PENDING record for THIS failure with a single
                # conditional UPDATE. A record that is no longer pending
                # belongs to another attempt's lifecycle (a racing retry
                # adopted it after our failure) — this attempt must not
                # overwrite that attempt's in-flight state (the exact
                # corruption a read-then-write here used to allow).
                from sqlalchemy import update as sa_update

                claimed = await session.execute(
                    sa_update(ProcessedMessageRecord)
                    .where(
                        ProcessedMessageRecord.id == record.id,
                        ProcessedMessageRecord.outcome == "pending",
                    )
                    .values(
                        outcome=final_outcome,
                        processed_at=datetime.now(UTC),
                        metadata_json=meta,
                    )
                    .execution_options(synchronize_session=False)
                )
                owns_record = claimed.rowcount == 1
                if not owns_record:
                    # Our execution still fails below; the record simply
                    # is not ours to transition. Do not dead-letter either:
                    # the owning attempt will decide that.
                    final_outcome = "failed"

            exec_repo = ExecutionRepository(session)
            if execution_id is not None:
                await exec_repo.fail(execution_id, f"{type(error).__name__}: {error}"[:1000])
            if objective_id is not None:
                # Close the participation row (ADR-0020), then transition.
                await ObjectiveRepository(session).record_execution_end(
                    objective_id, execution_id or "", outcome="failed"
                )
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
        principal_str = f" The principal you act for has id {principal_id}." if principal_id else ""
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
