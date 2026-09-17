"""RuntimeBridge — the core intelligence loop.

The bridge is the heart of WAX. It coordinates:

    message → identity → idempotency → execution
           → memory retrieval → context assembly
           → model ↔ terminal loop
           → memory extraction → delivery

This is a FULL REWRITE from the old capability/authority architecture.
The bridge no longer:
- constructs capability tool catalogues
- routes through authority/approval gates
- tracks resource budgets
- manages objectives
- provisions workspaces
- invokes a capability registry

The bridge now exposes ONE tool to the model: `terminal`. The terminal
is the universal environment interface. The intelligence uses it to
inspect files, execute programs, use the network, create services, etc.
Observations come back and become part of the next model context.

This is an open-world architecture: the runtime provides the environment;
the intelligence decides how to operate it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wax.continuity.repository import ConversationRepository
from wax.execution.contracts import ExecutionKind
from wax.execution.repository import ExecutionRepository
from wax.identity.repository import PrincipalRepository
from wax.intelligence.contracts import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
    ToolCall,
    ToolSpec,
)
from wax.intelligence.service import IntelligenceService
from wax.memory.contracts import MemoryKind
from wax.memory.repository import MemoryRepository
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeResponseStatus,
)
from wax.runtime.executor import (
    TerminalExecutor,
    cleanup_execution_workspace,
    create_execution_workspace,
)
from wax.runtime.logging import get_logger
from wax.runtime.services import RuntimeServices
from wax.state.bridge_models import ProcessedMessageRecord

log = get_logger(__name__)

# The single tool the model can request. The terminal is the environment
# interface — not a registered capability, not a tool among many.
TERMINAL_TOOL_SPEC = ToolSpec(
    name="terminal",
    description=(
        "Execute a command in the terminal environment. The command runs in "
        "a shell (bash -c) with full environment access (env vars, network, "
        "filesystem). Use this to inspect files, run programs, install "
        "software, call APIs, start services, etc. The working directory "
        "persists across terminal calls within the same conversation. "
        "Always observe the output before claiming an action succeeded."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "mode": {
                "type": "string",
                "enum": ["foreground", "detached"],
                "description": (
                    "foreground (default): wait for the command to finish and "
                    "return stdout/stderr/exit_code. detached: start the "
                    "command as a background process and return immediately "
                    "(use for servers, workers, long-running services)."
                ),
                "default": "foreground",
            },
            "timeout": {
                "type": "number",
                "description": "Optional timeout in seconds (foreground only). Default 60.",
            },
        },
        "required": ["command"],
    },
)

# The system prompt is intentionally small. It tells the intelligence what
# it is, what it has, and how to operate. It does NOT enumerate capabilities,
# permissions, approval flows, or domain-specific behavior.
SYSTEM_PROMPT = """\
You are the intelligence operating inside WAX, an AI runtime.

You have access to a terminal environment. You can:
- inspect and modify files
- execute programs (python, node, git, curl, etc.)
- use the network (HTTP, APIs, git clone, etc.)
- install software where the environment permits
- start background services
- interact with the runtime through the terminal

The terminal is your universal interface to the environment. Use it to
investigate, act, and observe. The working directory persists across
your terminal calls within this conversation.

Relevant memories from past interactions are provided in the context.
Use them for continuity, but verify current state through the terminal
when it matters.

When you need to act, use the terminal tool. Observe the result before
claiming success. Continue until the user's request is actually resolved
or you have a clear reason to stop. Then produce your final response as
plain text — it will be delivered to the user.

Be honest about what you observe. Do not claim an action succeeded
without verifying it. If something fails, report the failure clearly.
"""


class RuntimeBridge:
    """The core intelligence loop.

    Constructed once at startup with the intelligence service and the
    runtime services container. Each call to `process()` handles one
    inbound message end-to-end.
    """

    def __init__(
        self,
        *,
        intelligence: IntelligenceService,
        services: RuntimeServices,
    ) -> None:
        self._intelligence = intelligence
        self._services = services

    async def process(self, session: AsyncSession, request: RuntimeRequest) -> RuntimeResponse:
        """Process one inbound message end-to-end."""
        now = datetime.now(UTC)

        # 1. Identity: who is this?
        principal = await PrincipalRepository(session).resolve_principal_by_credential(
            kind=f"{request.interface_kind.value}_phone"
            if request.interface_kind == InterfaceKind.WHATSAPP
            else request.interface_kind.value,
            value=request.sender_interface_id,
        )
        if principal is None:
            # Auto-create the principal on first contact. The runtime does
            # not require pre-registration; the interface identity IS the
            # identity. This is the open-world trust model.
            repo = PrincipalRepository(session)
            principal = await repo.create_principal(display_name=request.sender_display_name)
            await repo.add_credential(
                principal.id,
                kind=f"{request.interface_kind.value}_phone"
                if request.interface_kind == InterfaceKind.WHATSAPP
                else request.interface_kind.value,
                value=request.sender_interface_id,
                is_verified=True,
            )
            await session.flush()

        # 2. Idempotency: have we already processed this message?
        existing = await session.execute(
            select(ProcessedMessageRecord).where(
                ProcessedMessageRecord.interface_message_id == request.interface_message_id
            )
        )
        existing_record = existing.scalar_one_or_none()
        if existing_record is not None:
            return RuntimeResponse(
                status=RuntimeResponseStatus.DUPLICATE,
                processed_at=now,
                principal_id=principal.id,
                duplicate_of_execution_id=existing_record.execution_id,
            )

        # 3. Execution: what is happening right now?
        exec_repo = ExecutionRepository(session)
        execution = await exec_repo.create(
            principal_id=principal.id,
            kind=ExecutionKind.SINGLE_TURN,
            objective=request.effective_text[:500] if request.effective_text else "(empty)",
        )
        await exec_repo.start(execution.id)

        # Record the processed message (idempotency).
        session.add(
            ProcessedMessageRecord(
                id=execution.id,  # use execution ID as the processed-message ID
                interface_message_id=request.interface_message_id,
                interface_kind=request.interface_kind.value,
                principal_id=principal.id,
                execution_id=execution.id,
                request_text=request.effective_text[:2000],
                received_at=request.received_at,
                outcome="processing",
            )
        )
        await session.flush()

        # 4. Conversation continuity
        conv_repo = ConversationRepository(session)
        conversation = await conv_repo.get_active_for_principal(principal.id)
        if conversation is None:
            conversation = await conv_repo.create(
                principal_id=principal.id,
                interface_kind=request.interface_kind.value,
            )
        await conv_repo.touch(conversation.id)

        # 5. Run the intelligence ↔ terminal loop
        try:
            response_text = await self._run_intelligence_loop(
                session, execution.id, principal.id, conversation.id, request
            )
            await exec_repo.complete(execution.id)
            # Update the processed message outcome
            await session.execute(
                select(ProcessedMessageRecord).where(ProcessedMessageRecord.id == execution.id)
            )
            pm = (
                await session.execute(
                    select(ProcessedMessageRecord).where(ProcessedMessageRecord.id == execution.id)
                )
            ).scalar_one()
            pm.outcome = "success"
            await session.flush()

            return RuntimeResponse(
                status=RuntimeResponseStatus.SUCCESS,
                text=response_text,
                execution_id=execution.id,
                principal_id=principal.id,
                processed_at=datetime.now(UTC),
            )
        except Exception as e:
            log.error(
                "bridge.process.error",
                execution_id=execution.id,
                error=str(e),
                error_type=type(e).__name__,
            )
            await exec_repo.fail(execution.id, f"{type(e).__name__}: {e}"[:1000])
            return RuntimeResponse(
                status=RuntimeResponseStatus.INTERNAL_ERROR,
                error=str(e),
                execution_id=execution.id,
                principal_id=principal.id,
                processed_at=datetime.now(UTC),
            )

    async def _run_intelligence_loop(
        self,
        session: AsyncSession,
        execution_id: str,
        principal_id: str,
        conversation_id: str,
        request: RuntimeRequest,
    ) -> str:
        """The core loop: model ↔ terminal until the model finishes."""
        exec_repo = ExecutionRepository(session)
        max_rounds = self._services.settings.terminal_max_rounds

        # Create the terminal executor with a persistent working directory
        # for this execution.
        workspace = create_execution_workspace(
            self._services.settings.terminal_working_dir_root,
            execution_id=execution_id,
        )
        executor = TerminalExecutor(
            working_dir=workspace,
            timeout_seconds=self._services.settings.terminal_timeout_seconds,
            output_max_chars=self._services.settings.terminal_output_max_chars,
        )

        # Build the initial message list: system + memory context + user message
        messages = await self._build_initial_messages(session, principal_id, request)

        try:
            for round_num in range(max_rounds):
                # Record the model call as an execution step
                await exec_repo.record_step(
                    execution_id=execution_id,
                    kind="model",
                    capability_name=None,
                    inputs={"round": round_num, "message_count": len(messages)},
                )

                # Call the intelligence
                response = await self._intelligence.complete(
                    LLMRequest(
                        messages=messages,
                        tools=[TERMINAL_TOOL_SPEC],
                        request_id=execution_id,
                    )
                )

                # Record the model response
                await exec_repo.record_step(
                    execution_id=execution_id,
                    kind="model_response",
                    capability_name=None,
                    inputs={
                        "content": response.content[:2000],
                        "finish_reason": response.finish_reason,
                        "tool_calls": len(response.tool_calls),
                    },
                )

                # If the model didn't request a terminal call, we're done.
                if not response.tool_calls:
                    # Automatic memory extraction
                    await self._extract_memory(
                        session, principal_id, execution_id, request, response
                    )
                    return response.content

                # Process terminal tool calls
                messages.append(
                    LLMMessage(
                        role=MessageRole.ASSISTANT,
                        content=response.content,
                        tool_calls=response.tool_calls,
                    )
                )

                for tool_call in response.tool_calls:
                    if tool_call.name == "terminal":
                        observation = await self._execute_terminal(
                            executor, tool_call, execution_id, exec_repo
                        )
                        messages.append(
                            LLMMessage(
                                role=MessageRole.TOOL,
                                content=observation,
                                tool_call_id=tool_call.id,
                                name="terminal",
                            )
                        )
                    else:
                        # Unknown tool — honest refusal
                        messages.append(
                            LLMMessage(
                                role=MessageRole.TOOL,
                                content=f"[error] unknown tool: {tool_call.name}",
                                tool_call_id=tool_call.id,
                                name=tool_call.name,
                            )
                        )

            # Max rounds exhausted
            log.warning("bridge.max_rounds_exhausted", execution_id=execution_id)
            return await self._force_final_response(messages, execution_id)
        finally:
            await executor.cleanup_detached()
            cleanup_execution_workspace(workspace)

    async def _build_initial_messages(
        self,
        session: AsyncSession,
        principal_id: str,
        request: RuntimeRequest,
    ) -> list[LLMMessage]:
        """Build the initial message list: system + memory context + user."""
        # Automatic memory retrieval — the runtime retrieves relevant
        # memories BEFORE the model runs. The model does not have to
        # call memory.search; the runtime does it for it.
        memory_repo = MemoryRepository(session)
        memories = await memory_repo.search_relevant(
            principal_id=principal_id,
            query=request.effective_text,
            limit=10,
        )

        system_content = SYSTEM_PROMPT
        if memories:
            memory_block = "\n\nRelevant memories from past interactions:\n"
            for m in memories:
                confidence_tag = f" (confidence={m.confidence:.1f})" if m.confidence < 1.0 else ""
                memory_block += f"\n- [{m.kind}] {m.content}{confidence_tag}"
            system_content += memory_block

        messages = [
            LLMMessage(role=MessageRole.SYSTEM, content=system_content),
            LLMMessage(role=MessageRole.USER, content=request.effective_text),
        ]
        return messages

    async def _execute_terminal(
        self,
        executor: TerminalExecutor,
        tool_call: ToolCall,
        execution_id: str,
        exec_repo: ExecutionRepository,
    ) -> str:
        """Execute a terminal tool call and return the observation string."""
        command = tool_call.arguments.get("command", "")
        mode = tool_call.arguments.get("mode", "foreground")
        timeout = tool_call.arguments.get("timeout")

        if not isinstance(command, str) or not command.strip():
            return "[error] command is required and must be a non-empty string"

        # Record the terminal action as an execution step
        await exec_repo.record_step(
            execution_id=execution_id,
            kind="terminal",
            capability_name="terminal",
            inputs={"command": command[:500], "mode": mode},
        )

        try:
            if mode == "detached":
                result = await executor.execute_detached(command)
            else:
                result = await executor.execute(command, timeout=timeout)

            # Record the observation
            await exec_repo.record_step(
                execution_id=execution_id,
                kind="terminal_observation",
                capability_name="terminal",
                inputs={
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "truncated": result.truncated,
                    "duration_seconds": result.duration_seconds,
                    "stdout_preview": result.stdout[:500],
                    "stderr_preview": result.stderr[:500],
                },
            )

            return result.to_observation()
        except Exception as e:
            error_msg = f"[terminal error] {type(e).__name__}: {e}"
            await exec_repo.record_step(
                execution_id=execution_id,
                kind="terminal_error",
                capability_name="terminal",
                inputs={"error": str(e)[:1000]},
            )
            return error_msg

    async def _extract_memory(
        self,
        session: AsyncSession,
        principal_id: str,
        execution_id: str,
        request: RuntimeRequest,
        response: LLMResponse,
    ) -> None:
        """Automatic memory extraction after the interaction.

        The runtime evaluates whether anything from this interaction
        deserves to be remembered. This is automatic — the model does
        not have to call memory.store. The extraction is conservative:
        only genuinely useful information becomes memory.
        """
        # Heuristic: if the user's message is short and the response is
        # short, this is likely a trivial exchange — don't store memory.
        # Real memory extraction would use a smaller model call to
        # evaluate salience, but for now we use a simple heuristic.
        user_text = request.effective_text.strip()
        response_text = response.content.strip()

        if len(user_text) < 20 or len(response_text) < 50:
            return

        # Store an episodic memory of the interaction
        memory_repo = MemoryRepository(session)
        from wax.memory.contracts import MemoryCreate

        await memory_repo.create(
            MemoryCreate(
                principal_id=principal_id,
                kind=MemoryKind.EPISODIC,
                content=f"User asked: {user_text[:500]}\n\nResponse: {response_text[:1000]}",
                provenance="user_interaction",
                source_execution_id=execution_id,
                confidence=1.0,
                importance=0.5,
                observed_at=datetime.now(UTC),
            )
        )
        log.info("memory.auto_extracted", execution_id=execution_id, principal_id=principal_id)

    async def _force_final_response(self, messages: list[LLMMessage], execution_id: str) -> str:
        """Force a final text response when max rounds is exhausted."""
        messages.append(
            LLMMessage(
                role=MessageRole.USER,
                content=(
                    "[runtime] You have reached the maximum number of terminal "
                    "rounds for this conversation. Please provide your final "
                    "response to the user now, summarizing what you've done "
                    "and any remaining steps."
                ),
            )
        )
        response = await self._intelligence.complete(
            LLMRequest(messages=messages, tools=None, request_id=execution_id)
        )
        return response.content

    async def run_reentry(self, work_item: Any) -> str:
        """Re-enter the intelligence loop from durable work.

        Called by the work runner when a scheduled work item wakes. The
        runtime reconstructs the context (memory + execution history) and
        continues the intelligence loop.
        """
        # For now, a simple re-entry that produces a continuation prompt.
        # Full re-entry with execution recovery is a future enhancement.
        return "[reentry] work item woke the runtime — continue from here."
