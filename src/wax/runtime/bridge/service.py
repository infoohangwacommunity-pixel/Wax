"""RuntimeBridge — the canonical entrypoint for interface messages.

The bridge:
1. Idempotency check: have we seen (interface_kind, interface_message_id)?
   If yes, return DUPLICATE with the original execution_id.
2. Identity resolution: resolve sender_interface_id → WAX Principal.
   On first contact, create a principal + credential automatically.
3. Persist a ProcessedMessageRecord (the idempotency lock).
4. Create an Objective (Phase L) for the message.
5. Start an Execution (Phase H) for the objective.
6. Call the IntelligenceService (Phase K) with the request text + memory.
7. Persist the response text in the ProcessedMessageRecord.
8. Return a RuntimeResponse to the interface adapter.

INVARIANTS:
- One interface message → at most one execution (dedup via unique constraint)
- The bridge never sees WhatsApp-specific shapes; it sees RuntimeRequest
- The bridge never makes authorization decisions; that's Authority's job
- The bridge never calls capabilities directly; that's the Invoker's job
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.execution.contracts import ExecutionKind
from wax.execution.repository import ExecutionRepository
from wax.identity.contracts import ALLOWED_CREDENTIAL_KINDS
from wax.identity.repository import PrincipalRepository
from wax.intelligence.contracts import (
    LLMMessage,
    LLMRequest,
    MessageRole,
)
from wax.intelligence.service import IntelligenceService
from wax.memory.contracts import MemoryCreate, MemoryKind
from wax.memory.repository import MemoryRepository
from wax.objective.contracts import ObjectiveCreate, ObjectiveKind
from wax.objective.repository import ObjectiveRepository
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeResponseStatus,
)
from wax.runtime.logging import get_logger
from wax.state.bridge_models import ProcessedMessageRecord

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


class RuntimeBridge:
    """The canonical entrypoint for interface messages.

    Use:
        bridge = RuntimeBridge(intelligence=intel_svc)
        async with db_session() as session:
            response = await bridge.process(session, request)
        # response.text → send back via the interface adapter
    """

    def __init__(
        self,
        intelligence: IntelligenceService,
        max_response_chars: int = 3500,
    ) -> None:
        self._intelligence = intelligence
        # WhatsApp text messages are limited to 4096 chars; we cap conservatively.
        self._max_response_chars = max_response_chars

    async def process(
        self,
        session: AsyncSession,
        request: RuntimeRequest,
    ) -> RuntimeResponse:
        """Process a RuntimeRequest end-to-end.

        This is the ONE method interface adapters call. They get back a
        RuntimeResponse that they translate to their platform shape.
        """
        # 1. Idempotency check
        existing = await self._find_existing(session, request)
        if existing is not None:
            log.info(
                "bridge.duplicate",
                interface=request.interface_kind.value,
                message_id=request.interface_message_id,
                original_execution=existing.execution_id,
            )
            return RuntimeResponse(
                status=RuntimeResponseStatus.DUPLICATE,
                execution_id=existing.execution_id,
                objective_id=existing.objective_id,
                principal_id=existing.principal_id,
                text=existing.response_text,
                processed_at=datetime.now(timezone.utc),
                duplicate_of_execution_id=existing.execution_id,
            )

        # 2. Identity resolution
        principal = await self._resolve_or_create_principal(session, request)
        if principal is None:
            return self._failure(
                RuntimeResponseStatus.PRINCIPAL_UNAUTHORIZED,
                "Could not resolve or create principal",
            )

        # 3. Persist the ProcessedMessageRecord (the idempotency lock)
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
        await session.flush()  # acquire the unique constraint

        try:
            # 4. Create an Objective
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

            # 5. Start an Execution
            exec_repo = ExecutionRepository(session)
            execution = await exec_repo.create(
                principal_id=principal.id,
                kind=ExecutionKind.SINGLE_TURN,
                objective=request.effective_text or "[empty message]",
            )
            await exec_repo.start(execution.id)
            await objective_repo.attach_execution(objective.id, execution.id)
            record.execution_id = execution.id

            # 6. Retrieve recent memory for context
            memory_repo = MemoryRepository(session)
            recent_memories = await memory_repo.list_active_for_principal(
                principal.id, kind="episodic", limit=5
            )

            # 7. Build the LLM request
            system_prompt = self._build_system_prompt(
                principal_display=principal.display_name,
                memory_count=len(recent_memories),
            )
            messages: list[LLMMessage] = [
                LLMMessage(role=MessageRole.SYSTEM, content=system_prompt),
            ]

            # Include a short summary of recent memory
            for m in reversed(recent_memories):
                summary = m.summary or str(m.content)[:200]
                messages.append(
                    LLMMessage(
                        role=MessageRole.SYSTEM,
                        content=f"[prior memory: {summary}]",
                    )
                )

            messages.append(
                LLMMessage(role=MessageRole.USER, content=request.effective_text)
            )

            llm_response = await self._intelligence.complete(
                LLMRequest(messages=messages, request_id=execution.id)
            )

            # Truncate for interface limits
            response_text = llm_response.content[: self._max_response_chars]

            # 8. Record a memory of this interaction
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

            # 9. Complete the execution
            await exec_repo.complete(
                execution.id,
                checkpoint={"response": response_text[:1000]},
            )

            # 10. Update the idempotency record
            record.outcome = "success"
            record.response_text = response_text
            record.processed_at = datetime.now(timezone.utc)

            await session.commit()

            return RuntimeResponse(
                status=RuntimeResponseStatus.SUCCESS,
                text=response_text,
                execution_id=execution.id,
                objective_id=objective.id,
                principal_id=principal.id,
                processed_at=datetime.now(timezone.utc),
            )

        except Exception as e:
            # Roll back the work but keep the idempotency record (so we
            # don't retry forever on the same message).
            record.outcome = "internal_error"
            record.response_text = None
            record.processed_at = datetime.now(timezone.utc)
            record.metadata_json = {"error": str(e)[:500], "type": type(e).__name__}
            await session.commit()

            log.error(
                "bridge.process.error",
                interface=request.interface_kind.value,
                message_id=request.interface_message_id,
                error=str(e),
                error_type=type(e).__name__,
            )

            return self._failure(
                RuntimeResponseStatus.INTERNAL_ERROR,
                f"Internal error: {type(e).__name__}",
            )

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

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
    ):
        """Resolve sender_interface_id → Principal. Create on first contact.

        Returns None only if the credential kind is invalid.
        """
        credential_kind = _INTERFACE_CREDENTIAL_KIND.get(request.interface_kind)
        if credential_kind is None:
            log.error(
                "bridge.unknown_interface",
                interface=request.interface_kind.value,
            )
            return None
        if credential_kind not in ALLOWED_CREDENTIAL_KINDS:
            log.error(
                "bridge.unknown_credential_kind",
                kind=credential_kind,
                interface=request.interface_kind.value,
            )
            return None

        repo = PrincipalRepository(session)
        principal = await repo.resolve_principal_by_credential(
            credential_kind, request.sender_interface_id
        )
        if principal is not None:
            return principal

        # First contact — create principal + credential
        principal = await repo.create_principal(
            display_name=request.sender_display_name
        )
        await repo.add_credential(
            principal.id,
            kind=credential_kind,
            value=request.sender_interface_id,
            is_verified=True,  # the interface vouched for this ID
        )
        log.info(
            "bridge.principal_created",
            principal_id=principal.id,
            interface=request.interface_kind.value,
            credential_kind=credential_kind,
        )
        return principal

    def _build_system_prompt(
        self, *, principal_display: str | None, memory_count: int
    ) -> str:
        """Build the system prompt for the LLM.

        This is intentionally minimal. WAX does NOT hardcode a tutor
        persona (Directive §5.1, §10). The AI determines its own behavior
        based on the user's objective.
        """
        name_str = f" The user's name is {principal_display}." if principal_display else ""
        memory_str = (
            f" You have {memory_count} recent memories about this user."
            if memory_count > 0
            else ""
        )
        return (
            "You are an AI operating inside the WAX runtime. You are intelligent; "
            "WAX is the environment that holds memory, identity, capabilities, and "
            "authorization on your behalf." + name_str + memory_str +
            " Help the user pursue their objective. Be concise and useful. If you "
            "need a capability you don't have, say so explicitly rather than "
            "fabricating."
        )

    def _failure(
        self, status: RuntimeResponseStatus, error: str
    ) -> RuntimeResponse:
        return RuntimeResponse(
            status=status,
            error=error,
            processed_at=datetime.now(timezone.utc),
        )
