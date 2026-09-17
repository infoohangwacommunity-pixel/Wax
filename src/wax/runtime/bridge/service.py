"""RuntimeBridge — the core intelligence loop.

The bridge is the brainstem. It stays small. The flow:

1. Receive message
2. Resolve identity
3. Rate limit (infrastructure protection, NOT authority)
4. Load memories (automatic)
5. Build context (enriched system prompt + memory + user message)
6. Call AI
7. Execute terminal if requested
8. Repeat until finished
9. Deliver response
10. Extract memories (automatic, LLM-driven)
11. Finish

The bridge exposes ONE tool to the model: `terminal`. The terminal is
the universal environment interface. Observations come back and become
part of the next model context.

Durable re-entry: when the work runner wakes a scheduled task, the bridge
rebuilds full context (memory + execution history + workspace state) and
runs the SAME intelligence loop — the AI thinks again, acts, and delivers.
This is not a stub. This is real re-entry.
"""

from __future__ import annotations

import os
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
    MessageRole,
    ToolCall,
    ToolSpec,
)
from wax.intelligence.service import IntelligenceService
from wax.memory.extraction import extract_memories
from wax.memory.repository import MemoryRepository
from wax.runtime.bridge.contracts import (
    InterfaceKind,
    RuntimeRequest,
    RuntimeResponse,
    RuntimeResponseStatus,
)
from wax.runtime.cost_protection import CostProtector
from wax.runtime.executor import (
    TerminalExecutor,
    execution_workspace,
    list_principal_files,
    principal_workspace,
)
from wax.runtime.logging import get_logger
from wax.runtime.rate_limit import RateLimiter
from wax.runtime.services import RuntimeServices
from wax.runtime.work.reentry import ReentryRequest, ReentryResult
from wax.state.bridge_models import ProcessedMessageRecord
from wax.state.memory_models import MemoryRecord

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
        "persists across terminal calls within this conversation. "
        "Always observe the output before claiming an action succeeded.\n\n"
        "A helper module `wax_runtime` is available with schedule(), "
        "remember(), and recall() functions for common WAX interactions."
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

# The base system prompt — kept small. Operational context (time, identity,
# workspace, env vars, helper usage) is appended at runtime per-execution.
BASE_SYSTEM_PROMPT = """\
You are the intelligence operating inside WAX, an AI runtime.

You have access to a terminal environment. You can:
- inspect and modify files
- execute programs (python, node, git, curl, etc.)
- use the network (HTTP, APIs, git clone, etc.)
- install software where the environment permits
- start background services
- interact with the runtime through the terminal

The terminal is your universal interface to the environment. Use it to
investigate, act, and observe. Your working directory persists across
terminal calls within this conversation, and your principal's workspace
persists across conversations.

Relevant memories from past interactions are provided in the context.
Use them for continuity, but verify current state through the terminal
when it matters.

When you need to act, use the terminal tool. Observe the result before
claiming success. Continue until the user's request is actually resolved
or you have a clear reason to stop. Then produce your final response as
plain text — it will be delivered to the user.

Be honest about what you observe. Do not claim an action succeeded
without verifying it. If something fails, report the failure clearly.

A helper module `wax_runtime` is available inside the terminal. Use it
for common WAX interactions:
- import wax_runtime; wax_runtime.schedule(prompt="...", wake_in_seconds=3600)
  → schedule durable work that wakes you later
- import wax_runtime; wax_runtime.remember(content="...", kind="fact")
  → store a structured memory explicitly
- import wax_runtime; wax_runtime.recall(query="...", limit=5)
  → retrieve memories matching a query
"""


class RuntimeBridge:
    """The core intelligence loop.

    Constructed once at startup with the intelligence service and the
    runtime services container. Each call to `process()` handles one
    inbound message end-to-end. `run_reentry()` handles a scheduled wake.
    """

    def __init__(
        self,
        *,
        intelligence: IntelligenceService,
        services: RuntimeServices,
    ) -> None:
        self._intelligence = intelligence
        self._services = services
        self._rate_limiter = RateLimiter(
            max_messages_per_hour=getattr(services.settings, "rate_limit_messages_per_hour", 30),
        )
        self._cost_protector = CostProtector(
            daily_budget_cents=getattr(services.settings, "daily_cost_budget_cents", 500),
        )

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

        # 2. Rate limit (infrastructure protection, NOT authority)
        if not self._rate_limiter.check(principal.id):
            log.warning("bridge.rate_limited", principal_id=principal.id)
            return RuntimeResponse(
                status=RuntimeResponseStatus.RATE_LIMITED,
                text="You're sending messages too quickly. Please wait a moment and try again.",
                principal_id=principal.id,
                processed_at=now,
            )

        # 2b. Cost protection (Part 25) — lightweight global spending cap
        if not self._cost_protector.can_spend(principal.id):
            log.warning("bridge.cost_exceeded", principal_id=principal.id)
            return RuntimeResponse(
                status=RuntimeResponseStatus.RATE_LIMITED,
                text="Daily message limit reached. Please try again tomorrow.",
                principal_id=principal.id,
                processed_at=now,
            )

        # 3. Idempotency: have we already processed this message?
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

        # 4. Execution: what is happening right now?
        exec_repo = ExecutionRepository(session)
        execution = await exec_repo.create(
            principal_id=principal.id,
            kind=ExecutionKind.SINGLE_TURN,
            objective=request.effective_text[:500] if request.effective_text else "(empty)",
        )
        await exec_repo.start(execution.id)

        session.add(
            ProcessedMessageRecord(
                id=execution.id,
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

        # 5. Conversation continuity
        conv_repo = ConversationRepository(session)
        conversation = await conv_repo.get_active_for_principal(principal.id)
        if conversation is None:
            conversation = await conv_repo.create(
                principal_id=principal.id,
                interface_kind=request.interface_kind.value,
            )
        await conv_repo.touch(conversation.id)

        # 6. Run the intelligence ↔ terminal loop
        try:
            response_text = await self._run_intelligence_loop(
                session,
                execution.id,
                principal.id,
                conversation.id,
                request.interface_kind.value,
                user_message=request.effective_text,
                reentry_context=None,
            )
            await exec_repo.complete(execution.id)
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

    # ===================================================================
    # Durable re-entry — the REAL implementation (directive §7, Upgrade 1)
    # ===================================================================

    async def run_reentry(self, request: ReentryRequest) -> ReentryResult:
        """Re-enter the intelligence loop from durable work.

        Called by the work runner when a scheduled work item wakes. The
        runtime rebuilds full context (memory + execution history + workspace
        state) and runs the SAME intelligence loop as a live message. The AI
        thinks again, acts, and delivers — not because a hardcoded reminder
        fired, but because intelligence woke again.

        This is NOT a stub. This is real re-entry.
        """
        from wax.continuity.repository import ConversationRepository
        from wax.execution.contracts import ExecutionKind
        from wax.execution.repository import ExecutionRepository
        from wax.state.engine import db_session

        log.info(
            "intelligence.reentry.start",
            work_id=request.work_item_id,
            principal_id=request.principal_id,
            originating_execution_id=request.originating_execution_id,
        )

        async with db_session() as session:
            # Create a continuation execution
            exec_repo = ExecutionRepository(session)
            execution = await exec_repo.create(
                principal_id=request.principal_id,
                kind=ExecutionKind.SINGLE_TURN,
                objective=f"reentry: {request.prompt[:200]}",
            )
            await exec_repo.start(execution.id)

            # Get or create conversation
            conv_repo = ConversationRepository(session)
            conversation = await conv_repo.get_active_for_principal(request.principal_id)
            if conversation is None:
                conversation = await conv_repo.create(
                    principal_id=request.principal_id,
                    interface_kind="whatsapp",
                )
            await conv_repo.touch(conversation.id)

            # Build the reentry context — what woke the AI and why
            reentry_context = (
                f"[DURABLE WORK WOKE YOU]\n"
                f"Work item: {request.work_item_id}\n"
                f"Originating execution: {request.originating_execution_id}\n"
                f"Original prompt: {request.prompt}\n"
                f"Wake event: {request.observation.get('event', 'scheduled_wake')}\n"
                f"Woke at: {datetime.now(UTC).isoformat()}\n\n"
                f"You scheduled this wake earlier. Continue from where you left off. "
                f"Check your workspace, recall relevant memories, and act on the wake event."
            )

            try:
                response_text = await self._run_intelligence_loop(
                    session,
                    execution.id,
                    request.principal_id,
                    conversation.id,
                    "whatsapp",
                    user_message=reentry_context,
                    reentry_context=request.prompt,
                )
                await exec_repo.complete(execution.id)

                # Deliver the response via WhatsApp if we have a delivery interface
                delivery_text = response_text
                if delivery_text and self._services.delivery:
                    try:
                        # Resolve the principal's WhatsApp phone number
                        from wax.identity.repository import PrincipalRepository

                        principal = await PrincipalRepository(session).get_principal(
                            request.principal_id
                        )
                        if principal:
                            creds = await PrincipalRepository(session).list_credentials(
                                request.principal_id
                            )
                            phone = next(
                                (c.value for c in creds if c.kind == "whatsapp_phone"), None
                            )
                            if phone:
                                await self._services.delivery.send(
                                    "whatsapp",
                                    recipient=phone,
                                    text=delivery_text,
                                )
                                log.info(
                                    "intelligence.reentry.delivered",
                                    work_id=request.work_item_id,
                                    to=phone,
                                )
                    except Exception as e:
                        log.warning(
                            "intelligence.reentry.delivery_failed",
                            work_id=request.work_item_id,
                            error=str(e)[:300],
                        )

                await session.commit()

                return ReentryResult(
                    execution_id=execution.id,
                    objective_id="",  # no objective subsystem
                    conversation_id=conversation.id,
                    outcome="succeeded",
                    response_text=delivery_text,
                )
            except Exception as e:
                log.error(
                    "intelligence.reentry.failed",
                    work_id=request.work_item_id,
                    error=str(e)[:500],
                    error_type=type(e).__name__,
                )
                await exec_repo.fail(execution.id, f"{type(e).__name__}: {e}"[:1000])
                await session.commit()
                return ReentryResult(
                    execution_id=execution.id,
                    objective_id="",
                    conversation_id=conversation.id,
                    outcome="failed",
                    error=str(e)[:500],
                )

    # ===================================================================
    # The shared intelligence ↔ terminal loop (used by both paths)
    # ===================================================================

    async def _run_intelligence_loop(
        self,
        session: AsyncSession,
        execution_id: str,
        principal_id: str,
        conversation_id: str,
        interface_kind: str,
        *,
        user_message: str,
        reentry_context: str | None = None,
    ) -> str:
        """The core loop: model ↔ terminal until the model finishes.

        Shared by both `process()` (live message) and `run_reentry()`
        (scheduled wake). The only difference is the user_message content
        and whether reentry_context is set.
        """
        exec_repo = ExecutionRepository(session)
        max_rounds = self._services.settings.terminal_max_rounds

        # Persistent per-principal workspace + per-execution subdirectory
        ws_root = self._services.settings.terminal_working_dir_root
        principal_ws = principal_workspace(ws_root, principal_id)
        exec_ws = execution_workspace(ws_root, principal_id, execution_id)

        executor = TerminalExecutor(
            working_dir=exec_ws,
            timeout_seconds=self._services.settings.terminal_timeout_seconds,
            output_max_chars=self._services.settings.terminal_output_max_chars,
            env_overrides={
                "WAX_CURRENT_PRINCIPAL_ID": principal_id,
                "WAX_CURRENT_EXECUTION_ID": execution_id,
                "WAX_CURRENT_WORKSPACE": str(exec_ws),
                "WAX_CURRENT_PRINCIPAL_WORKSPACE": str(principal_ws),
            },
        )

        # Build the initial message list with enriched system prompt
        messages = await self._build_initial_messages(
            session, principal_id, principal_ws, exec_ws, user_message, reentry_context
        )

        # Track the user message and final response for memory extraction
        original_user_message = user_message
        final_response = ""

        try:
            for round_num in range(max_rounds):
                await exec_repo.record_step(
                    execution_id=execution_id,
                    kind="model",
                    capability_name=None,
                    inputs={"round": round_num, "message_count": len(messages)},
                )

                response = await self._intelligence.complete(
                    LLMRequest(
                        messages=messages,
                        tools=[TERMINAL_TOOL_SPEC],
                        request_id=execution_id,
                    )
                )

                # Record cost (Part 25) — lightweight global spending protection
                from wax.runtime.cost_protection import estimate_llm_cost_cents

                cost_cents = estimate_llm_cost_cents(
                    prompt_tokens=response.usage.get("tokens_prompt", 0),
                    completion_tokens=response.usage.get("tokens_completion", 0),
                )
                self._cost_protector.record_spend(principal_id, cost_cents)

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
                    final_response = response.content
                    break

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
                        messages.append(
                            LLMMessage(
                                role=MessageRole.TOOL,
                                content=f"[error] unknown tool: {tool_call.name}",
                                tool_call_id=tool_call.id,
                                name=tool_call.name,
                            )
                        )

                # Part 22: Checkpoint the intelligence loop state after each
                # terminal round. If the server crashes mid-execution, the
                # work runner can resume from this checkpoint — the AI wakes
                # up with the same message history and continues.
                await self._checkpoint_messages(exec_repo, execution_id, messages, round_num)
            else:
                # Max rounds exhausted — force a final response
                log.warning("bridge.max_rounds_exhausted", execution_id=execution_id)
                final_response = await self._force_final_response(messages, execution_id)

            # Automatic memory extraction (LLM-driven, structured)
            await extract_memories(
                session=session,
                intelligence=self._intelligence,
                principal_id=principal_id,
                execution_id=execution_id,
                user_message=original_user_message,
                ai_response=final_response,
            )

            return final_response
        finally:
            await executor.cleanup_detached()
            # NOTE: do NOT clean up the execution workspace — the principal's
            # workspace persists across conversations. Per-execution
            # subdirectories can be cleaned by a maintenance sweep later.

    async def _build_initial_messages(
        self,
        session: AsyncSession,
        principal_id: str,
        principal_ws: Any,
        exec_ws: Any,
        user_message: str,
        reentry_context: str | None,
    ) -> list[LLMMessage]:
        """Build the initial message list: enriched system + memory + user."""
        # Automatic memory retrieval
        memory_repo = MemoryRepository(session)
        memories = await memory_repo.search_relevant(
            principal_id=principal_id,
            query=user_message,
            limit=10,
        )

        # Parts 11-12: Surface active projects and waiting states
        active_projects = await self._fetch_active_projects(session, principal_id)
        waiting_states = await self._fetch_waiting_states(session, principal_id)

        # Build the enriched system prompt with operational context
        system_content = self._build_system_prompt(
            principal_id=principal_id,
            principal_ws=str(principal_ws),
            exec_ws=str(exec_ws),
            memories=memories,
            reentry_context=reentry_context,
            active_projects=active_projects,
            waiting_states=waiting_states,
        )

        return [
            LLMMessage(role=MessageRole.SYSTEM, content=system_content),
            LLMMessage(role=MessageRole.USER, content=user_message),
        ]

    async def _fetch_active_projects(
        self, session: AsyncSession, principal_id: str
    ) -> list[MemoryRecord]:
        """Fetch active project memories (Part 11) — living things being worked on."""
        result = await session.execute(
            select(MemoryRecord)
            .where(
                MemoryRecord.principal_id == principal_id,
                MemoryRecord.kind == "project",
                MemoryRecord.status == "active",
            )
            .order_by(MemoryRecord.updated_at.desc())
            .limit(5)
        )
        return list(result.scalars().all())

    async def _fetch_waiting_states(
        self, session: AsyncSession, principal_id: str
    ) -> list[MemoryRecord]:
        """Fetch waiting memories (Part 12) — things the AI is waiting for."""
        result = await session.execute(
            select(MemoryRecord)
            .where(
                MemoryRecord.principal_id == principal_id,
                MemoryRecord.kind == "waiting",
                MemoryRecord.status == "active",
            )
            .order_by(MemoryRecord.updated_at.desc())
            .limit(5)
        )
        return list(result.scalars().all())

    def _build_system_prompt(
        self,
        *,
        principal_id: str,
        principal_ws: str,
        exec_ws: str,
        memories: list[Any],
        reentry_context: str | None,
        active_projects: list[Any] | None = None,
        waiting_states: list[Any] | None = None,
    ) -> str:
        """Build the enriched system prompt with operational context.

        Per directive §15 / Upgrade 6: the AI should always know:
        - Current time
        - Student identity
        - Workspace path
        - Available environment variables (key names only)
        - How to use wax_runtime helper
        - Memory already loaded

        This saves the AI multiple reconnaissance terminal rounds.
        """
        now = datetime.now(UTC)
        parts = [BASE_SYSTEM_PROMPT]

        # Operational context
        parts.append("\n--- OPERATIONAL CONTEXT ---")
        parts.append(f"Current time: {now.isoformat()}")
        parts.append(f"Student ID: {principal_id}")
        parts.append(f"Principal workspace (persistent): {principal_ws}")
        parts.append(f"Execution workspace (this turn): {exec_ws}")

        # Available env var key names (NOT values — the AI reads them when needed)
        wax_env_vars = sorted(k for k in os.environ if k.startswith("WAX_"))
        parts.append(f"Available WAX env vars: {', '.join(wax_env_vars[:20])}")

        # List files already in the principal's workspace (so the AI sees what exists)
        ws_root = self._services.settings.terminal_working_dir_root
        existing_files = list_principal_files(ws_root, principal_id)
        if existing_files:
            parts.append(f"Existing files in your workspace: {', '.join(existing_files[:15])}")
        else:
            parts.append("Your workspace is empty — this is your first interaction here.")

        # wax_runtime helper reminder
        parts.append(
            "Helper: import wax_runtime; wax_runtime.schedule/remember/recall "
            "(reads WAX_CURRENT_PRINCIPAL_ID + WAX_DATABASE_URL from env)"
        )

        # Loaded memories
        if memories:
            parts.append("\n--- RELEVANT MEMORIES ---")
            for m in memories:
                confidence_tag = f" (confidence={m.confidence:.1f})" if m.confidence < 1.0 else ""
                content_text = m.content if isinstance(m.content, str) else str(m.content)
                parts.append(f"- [{m.kind}] {content_text}{confidence_tag}")

        # Active projects (Part 11) — living things being worked on
        if active_projects:
            parts.append("\n--- ACTIVE PROJECTS ---")
            for p in active_projects:
                p_content = p.content if isinstance(p.content, dict) else {"text": str(p.content)}
                p_text = p_content.get("text", str(p_content))
                parts.append(f"- [project] {p_text}")

        # Waiting states (Part 12) — things the AI is waiting for
        if waiting_states:
            parts.append("\n--- WAITING STATES ---")
            for w in waiting_states:
                w_content = w.content if isinstance(w.content, dict) else {"text": str(w.content)}
                w_text = w_content.get("text", str(w_content))
                parts.append(f"- [waiting] {w_text}")

        # Reentry context
        if reentry_context:
            parts.append(f"\n--- REENTRY CONTEXT ---\n{reentry_context}")

        return "\n".join(parts)

    # ===================================================================
    # Part 22: Execution recovery — checkpoint + resume
    # ===================================================================

    async def _checkpoint_messages(
        self,
        exec_repo: ExecutionRepository,
        execution_id: str,
        messages: list[LLMMessage],
        round_num: int,
    ) -> None:
        """Checkpoint the intelligence loop state after each terminal round.

        Serializes the full message history to the execution's checkpoint
        column. If the server crashes, the work runner can load this
        checkpoint and resume the loop — the AI wakes up with the same
        context and continues from where it stopped.
        """
        try:
            checkpoint = {
                "schema_version": 1,
                "round": round_num,
                "messages": self._serialize_messages(messages),
                "checkpointed_at": datetime.now(UTC).isoformat(),
            }
            await exec_repo.update_checkpoint(execution_id, checkpoint)
        except Exception as e:
            # Checkpoint failure must never break the execution — it's
            # a recovery optimization, not a correctness requirement.
            log.warning(
                "bridge.checkpoint_failed",
                execution_id=execution_id,
                error=str(e)[:200],
            )

    def _serialize_messages(self, messages: list[LLMMessage]) -> list[dict[str, Any]]:
        """Serialize LLMMessage list to JSON-safe dicts."""
        result: list[dict[str, Any]] = []
        for msg in messages:
            entry: dict[str, Any] = {
                "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                "content": msg.content,
            }
            if msg.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                    }
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            if msg.name:
                entry["name"] = msg.name
            result.append(entry)
        return result

    def _deserialize_messages(self, raw: list[dict[str, Any]]) -> list[LLMMessage]:
        """Reconstruct LLMMessage list from JSON dicts."""
        messages: list[LLMMessage] = []
        for entry in raw:
            role_str = entry.get("role", "user")
            try:
                role = MessageRole(role_str)
            except ValueError:
                role = MessageRole.USER

            tool_calls = None
            if entry.get("tool_calls"):
                tool_calls = [
                    ToolCall(
                        id=tc.get("id", ""),
                        name=tc.get("name", ""),
                        arguments=tc.get("arguments", {}),
                    )
                    for tc in entry["tool_calls"]
                ]

            messages.append(
                LLMMessage(
                    role=role,
                    content=entry.get("content", ""),
                    name=entry.get("name"),
                    tool_calls=tool_calls,
                    tool_call_id=entry.get("tool_call_id"),
                )
            )
        return messages

    async def resume_execution(self, execution_id: str) -> str | None:
        """Resume an interrupted execution from its checkpoint (Part 22).

        Called by the work runner on startup when it finds an execution
        in 'running' state with a terminal-loop checkpoint. The bridge:

        1. Loads the checkpoint (message history + round number)
        2. Reconstructs the workspace (persistent per-principal)
        3. Re-enters the intelligence loop from where it stopped
        4. The AI sees the same context and continues naturally

        The AI receives a [SYSTEM RECOVERY] notice so it knows the
        server restarted and its detached processes were killed.

        Returns the final response text, or None if no checkpoint exists.
        """
        from wax.execution.repository import ExecutionRepository
        from wax.state.engine import db_session

        async with db_session() as session:
            exec_repo = ExecutionRepository(session)
            execution = await exec_repo.get(execution_id)
            if execution is None:
                log.warning("bridge.resume_not_found", execution_id=execution_id)
                return None

            if not execution.checkpoint:
                log.info("bridge.resume_no_checkpoint", execution_id=execution_id)
                return None

            checkpoint = execution.checkpoint
            if not isinstance(checkpoint, dict) or "messages" not in checkpoint:
                log.info("bridge.resume_invalid_checkpoint", execution_id=execution_id)
                return None

            messages = self._deserialize_messages(checkpoint["messages"])
            round_num = int(checkpoint.get("round", 0))
            principal_id = execution.principal_id

            log.info(
                "bridge.resuming_execution",
                execution_id=execution_id,
                principal_id=principal_id,
                round=round_num,
                message_count=len(messages),
            )

            # Inject a recovery notice so the AI knows what happened
            messages.append(
                LLMMessage(
                    role=MessageRole.USER,
                    content=(
                        "[SYSTEM RECOVERY] The WAX server restarted while you "
                        "were working. Your message history and workspace files "
                        "are preserved. Any detached processes you started were "
                        "killed when the server stopped — restart them if needed. "
                        "Continue from where you left off. Round "
                        f"{round_num + 1}."
                    ),
                )
            )

            # Reconstruct the workspace
            ws_root = self._services.settings.terminal_working_dir_root
            principal_ws = principal_workspace(ws_root, principal_id)
            exec_ws = execution_workspace(ws_root, principal_id, execution_id)

            executor = TerminalExecutor(
                working_dir=exec_ws,
                timeout_seconds=self._services.settings.terminal_timeout_seconds,
                output_max_chars=self._services.settings.terminal_output_max_chars,
                env_overrides={
                    "WAX_CURRENT_PRINCIPAL_ID": principal_id,
                    "WAX_CURRENT_EXECUTION_ID": execution_id,
                    "WAX_CURRENT_WORKSPACE": str(exec_ws),
                    "WAX_CURRENT_PRINCIPAL_WORKSPACE": str(principal_ws),
                },
            )

            max_rounds = self._services.settings.terminal_max_rounds
            final_response = ""

            try:
                # Continue from the next round after the checkpoint
                for resume_round in range(round_num + 1, max_rounds):
                    await exec_repo.record_step(
                        execution_id=execution_id,
                        kind="model_recovery",
                        capability_name=None,
                        inputs={"round": resume_round, "recovered": True},
                    )

                    response = await self._intelligence.complete(
                        LLMRequest(
                            messages=messages,
                            tools=[TERMINAL_TOOL_SPEC],
                            request_id=execution_id,
                        )
                    )

                    from wax.runtime.cost_protection import estimate_llm_cost_cents

                    cost_cents = estimate_llm_cost_cents(
                        prompt_tokens=response.usage.get("tokens_prompt", 0),
                        completion_tokens=response.usage.get("tokens_completion", 0),
                    )
                    self._cost_protector.record_spend(principal_id, cost_cents)

                    if not response.tool_calls:
                        final_response = response.content
                        break

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
                            messages.append(
                                LLMMessage(
                                    role=MessageRole.TOOL,
                                    content=f"[error] unknown tool: {tool_call.name}",
                                    tool_call_id=tool_call.id,
                                    name=tool_call.name,
                                )
                            )

                    # Checkpoint again after each terminal round
                    await self._checkpoint_messages(exec_repo, execution_id, messages, resume_round)
                else:
                    log.warning("bridge.resume_max_rounds", execution_id=execution_id)
                    final_response = await self._force_final_response(messages, execution_id)

                # Complete the recovered execution
                await exec_repo.complete(execution_id)

                # Memory extraction for the recovered execution
                await extract_memories(
                    session=session,
                    intelligence=self._intelligence,
                    principal_id=principal_id,
                    execution_id=execution_id,
                    user_message="[recovered execution]",
                    ai_response=final_response,
                )

                await session.commit()

                log.info(
                    "bridge.resumed_execution_complete",
                    execution_id=execution_id,
                    principal_id=principal_id,
                )

                return final_response
            except Exception as e:
                log.error(
                    "bridge.resume_failed",
                    execution_id=execution_id,
                    error=str(e)[:500],
                    error_type=type(e).__name__,
                )
                await exec_repo.fail(execution_id, f"resume failed: {type(e).__name__}: {e}"[:1000])
                await session.commit()
                return None
            finally:
                await executor.cleanup_detached()

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
                    "network_calls": len(result.network_calls),
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
