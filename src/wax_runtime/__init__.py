"""Runtime helper — a tiny convenience layer for the AI.

This is NOT a capability system. It is NOT a tool registry. It is a
small helper module the AI can import from its terminal environment to
remove repetitive boilerplate for common WAX interactions:

- schedule work (wake me later)
- remember a fact
- retrieve memories

The AI can also do these things manually (write to the database, write
a file, etc.). The helper just makes the common cases one-liners.

The helper runs INSIDE the terminal subprocess — it reads WAX_DATABASE_URL
from the environment and connects directly. The AI imports it like:

    from wax_runtime import schedule, remember, recall

This module is installed as a console script + importable module. It is
deliberately tiny — under 200 LOC. If the AI needs something not here,
it composes it from the environment (that's the open-world philosophy).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any


def _get_db_url() -> str:
    url = os.environ.get("WAX_DATABASE_URL", "")
    if not url:
        raise RuntimeError("WAX_DATABASE_URL is not set in the environment")
    return url


def _async_run(coro: Any) -> Any:
    """Run an async coroutine from sync context (the terminal is sync)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an async context — make a new loop in a thread
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        pass
    return asyncio.run(coro)


def schedule(
    *,
    prompt: str,
    wake_at: datetime | None = None,
    wake_in_seconds: float | None = None,
    observation: dict[str, Any] | None = None,
    principal_id: str | None = None,
    execution_id: str | None = None,
) -> str:
    """Schedule durable work that wakes the intelligence later.

    Example:
        schedule(
            prompt="Check if the user's WAEC result has been uploaded.",
            wake_in_seconds=3600,
            principal_id=current_principal_id(),
        )

    The work item is durable — it survives restarts. When the wake time
    arrives, the work runner claims it and re-enters the intelligence
    with the prompt + observation. The AI thinks again and acts.

    Returns the work item ID.
    """
    if wake_at is None and wake_in_seconds is not None:
        wake_at = datetime.now(UTC) + timedelta(seconds=wake_in_seconds)
    if wake_at is None:
        raise ValueError("either wake_at or wake_in_seconds must be provided")
    if observation is None:
        observation = {"source": "runtime", "event": "scheduled_wake"}
    if principal_id is None:
        principal_id = os.environ.get("WAX_CURRENT_PRINCIPAL_ID", "")
    if not principal_id:
        raise ValueError("principal_id is required (set WAX_CURRENT_PRINCIPAL_ID or pass it)")
    if execution_id is None:
        execution_id = os.environ.get("WAX_CURRENT_EXECUTION_ID", "")

    from wax.core.config import WaxSettings
    from wax.runtime.work.repository import WorkRepository
    from wax.state.engine import db_session, init_engine

    async def _do() -> str:
        settings = WaxSettings()
        init_engine(settings)
        async with db_session() as session:
            repo = WorkRepository(session)
            item = await repo.schedule(
                kind="intelligence",
                payload={"prompt": prompt, "observation": observation},
                wake_at=wake_at,
                principal_id=principal_id,
                execution_id=execution_id or None,
                max_attempts=3,
            )
            await session.commit()
            return item.id

    return _async_run(_do())


def remember(
    *,
    content: str | dict[str, Any],
    kind: str = "fact",
    principal_id: str | None = None,
    execution_id: str | None = None,
    confidence: float = 1.0,
    importance: float = 0.5,
    provenance: str = "model_observation",
) -> str:
    """Store a structured memory.

    Kinds: fact, preference, skill, episodic, project, waiting, procedural

    Example:
        remember(
            content="User's WAEC exam is on October 15th.",
            kind="fact",
            confidence=1.0,
            importance=0.9,
        )

    Returns the memory ID.
    """
    if principal_id is None:
        principal_id = os.environ.get("WAX_CURRENT_PRINCIPAL_ID", "")
    if not principal_id:
        raise ValueError("principal_id is required")
    if execution_id is None:
        execution_id = os.environ.get("WAX_CURRENT_EXECUTION_ID") or None

    content_dict = {"text": content} if isinstance(content, str) else content

    from wax.core.config import WaxSettings
    from wax.memory.contracts import MemoryCreate, MemoryKind
    from wax.memory.repository import MemoryRepository
    from wax.state.engine import db_session, init_engine

    async def _do() -> str:
        settings = WaxSettings()
        init_engine(settings)
        async with db_session() as session:
            repo = MemoryRepository(session)
            try:
                kind_enum = MemoryKind(kind)
            except ValueError:
                kind_enum = MemoryKind.FACT
            record = await repo.create(
                MemoryCreate(
                    principal_id=principal_id,
                    kind=kind_enum,
                    content=content_dict,
                    provenance=provenance,
                    source_execution_id=execution_id,
                    confidence=confidence,
                    importance=importance,
                    observed_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return record.id

    return _async_run(_do())


def recall(
    *,
    query: str,
    principal_id: str | None = None,
    limit: int = 10,
    kinds: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Retrieve relevant memories.

    Combines semantic relevance + recency + importance (the runtime's
    retrieval engine handles the ranking). Optionally filter by kind.

    Example:
        memories = recall(
            query="exam preparation",
            kinds=["fact", "skill", "project"],
            limit=5,
        )
        for m in memories:
            print(m["kind"], m["content"])
    """
    if principal_id is None:
        principal_id = os.environ.get("WAX_CURRENT_PRINCIPAL_ID", "")
    if not principal_id:
        raise ValueError("principal_id is required")

    from wax.core.config import WaxSettings
    from wax.memory.repository import MemoryRepository
    from wax.state.engine import db_session, init_engine

    async def _do() -> list[dict[str, Any]]:
        settings = WaxSettings()
        init_engine(settings)
        async with db_session() as session:
            repo = MemoryRepository(session)
            memories = await repo.search_relevant(
                principal_id=principal_id,
                query=query,
                limit=limit,
            )
            result = []
            for m in memories:
                if kinds and m.kind not in kinds:
                    continue
                result.append(
                    {
                        "id": m.id,
                        "kind": m.kind,
                        "content": m.content
                        if isinstance(m.content, dict)
                        else {"text": str(m.content)},
                        "confidence": m.confidence,
                        "importance": m.importance,
                        "observed_at": m.observed_at.isoformat() if m.observed_at else None,
                    }
                )
            return result

    return _async_run(_do())


def forget(*, memory_id: str) -> bool:
    """Forget a memory (soft delete — excluded from all retrieval)."""
    from wax.core.config import WaxSettings
    from wax.memory.repository import MemoryRepository
    from wax.state.engine import db_session, init_engine

    async def _do() -> bool:
        settings = WaxSettings()
        init_engine(settings)
        async with db_session() as session:
            repo = MemoryRepository(session)
            ok = await repo.forget(memory_id)
            await session.commit()
            return ok

    return _async_run(_do())


def workspace_path() -> str:
    """Return the AI's persistent workspace path."""
    return os.environ.get("WAX_CURRENT_WORKSPACE", "./")


def serve_page(
    html_content: str,
    *,
    purpose: str = "",
    expires_in_minutes: int = 30,
    auto_wake: bool = True,
) -> str:
    """Serve a temporary interaction page and return the full URL.

    The AI creates an HTML page (form, upload, OAuth, secret input).
    The runtime hosts it via the main FastAPI app at /i/{token}. The
    student opens the URL, submits, and the AI continues.

    If auto_wake=True (default), a durable work item is scheduled that
    waits for the submission signal. When the student submits, the work
    item wakes the AI with the submission result in its context.

    Returns the full public URL the AI should send to the student.

    Example:
        url = serve_page(
            "<html><body><h1>Upload your WAEC result</h1>"
            "<form method='POST' enctype='multipart/form-data'>"
            "<input type='file' name='file'><button>Upload</button></form></body></html>",
            purpose="waec_upload",
        )
        # Send `url` to the student via WhatsApp
    """

    public_url = os.environ.get("WAX_PUBLIC_URL", "http://localhost:8000")
    principal = current_principal_id()
    execution = current_execution_id()

    # DB-backed session creation (Bug 3 fix — sessions must survive
    # process boundaries because serve_page runs in a terminal subprocess)
    async def _do():
        from wax.core.config import WaxSettings
        from wax.runtime.web_pages import create_session as db_create_session
        from wax.state.engine import db_session, init_engine

        settings = WaxSettings()
        init_engine(settings)
        async with db_session() as db:
            record = await db_create_session(
                session=db,
                purpose=purpose or "form",
                principal_id=principal,
                html_content=html_content,
                execution_id=execution or None,
                expires_in_minutes=expires_in_minutes,
                wake_event="interaction.submitted:",  # will append session_id
            )
            # Fix wake event with actual session ID
            record.wake_event = f"interaction.submitted:{record.id}"
            await db.commit()
            return record

    record = _async_run(_do())

    # Auto-wake: schedule a work item that waits for the submission signal
    if auto_wake and principal:
        with contextlib.suppress(Exception):
            schedule(
                prompt=f"The user submitted the {purpose or 'form'} you sent them. Check the result and continue.",
                observation={
                    "source": "runtime",
                    "event": f"interaction.submitted:{record.id}",
                },
                wake_in_seconds=expires_in_minutes * 60,
                principal_id=principal,
                execution_id=execution or None,
            )

    return public_url + f"/i/{record.secret_token}"


def create_context(
    *,
    label: str,
    state: dict | None = None,
    summary: str | None = None,
    aliases: list[str] | None = None,
    principal_id: str | None = None,
) -> str:
    """Create a new persistent context (project, goal, thread of meaning).

    The AI calls this when a student starts something new: "I want to
    work on my university application." The context persists across
    conversations and is surfaced automatically when the student
    mentions it later.

    Returns the context ID.
    """
    if principal_id is None:
        principal_id = current_principal_id()
    if not principal_id:
        raise ValueError("principal_id is required")

    from wax.continuity.context_repository import ContextRepository
    from wax.core.config import WaxSettings
    from wax.state.engine import db_session, init_engine

    async def _do() -> str:
        settings = WaxSettings()
        init_engine(settings)
        async with db_session() as session:
            repo = ContextRepository(session)
            ctx = await repo.create(
                principal_id=principal_id,
                label=label,
                state=state or {},
                summary=summary,
                aliases=aliases or [],
            )
            await session.commit()
            return ctx.id

    return _async_run(_do())


def update_context(
    *,
    context_id: str,
    summary: str | None = None,
    state: dict | None = None,
    status: str | None = None,
) -> bool:
    """Update a context's summary, state, or status.

    The AI calls this after meaningful progress on a context: "The
    application was submitted, now waiting for interview." The updated
    state is surfaced in future conversations automatically.

    Returns True if the update succeeded.
    """
    from wax.continuity.context_repository import ContextRepository
    from wax.core.config import WaxSettings
    from wax.state.engine import db_session, init_engine

    async def _do() -> bool:
        settings = WaxSettings()
        init_engine(settings)
        async with db_session() as session:
            repo = ContextRepository(session)
            ok = False
            if status:
                ok = await repo.set_status(context_id, status)
            if summary is not None:
                ok = await repo.update_summary(context_id, summary)
            if state is not None:
                ok = await repo.update_state(context_id, state)
            await session.commit()
            return ok

    return _async_run(_do())


def current_principal_id() -> str:
    """Return the current principal ID (set by the bridge in env)."""
    return os.environ.get("WAX_CURRENT_PRINCIPAL_ID", "")


def current_execution_id() -> str:
    """Return the current execution ID (set by the bridge in env)."""
    return os.environ.get("WAX_CURRENT_EXECUTION_ID", "")


__all__ = [
    "create_context",
    "current_execution_id",
    "current_principal_id",
    "forget",
    "recall",
    "remember",
    "schedule",
    "serve_page",
    "update_context",
    "workspace_path",
]
