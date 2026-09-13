"""Memory repository — data access for the memory system.

Repository pattern isolates the ORM choice. Memory operations:
- create: store a new memory
- get: retrieve by ID
- list_for_principal: paginated retrieval of a principal's active memories
- supersede: mark a memory as replaced by a newer one (provenance-preserving)
- forget: mark a memory as forgotten (soft delete; record retained for audit)
- expire_due: find memories whose retention policy has fired

Note on "forget": per Directive §34, forgetting is a real operation.
A forgotten memory is not deleted from the DB (we keep the audit trail),
but it is excluded from default retrieval.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from wax.memory.contracts import MemoryCreate, MemoryKind, MemoryStatus
from wax.runtime.logging import get_logger
from wax.state.memory_models import MemoryRecord

log = get_logger(__name__)


def _new_ulid() -> str:
    return str(ULID())


class MemoryRepository:
    """Data access for memory records.

    Each method takes an AsyncSession (caller-managed transaction). The
    repository does not commit — that's the caller's responsibility.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, payload: MemoryCreate) -> MemoryRecord:
        """Store a new memory. Returns the unsaved-but-attached record."""
        record = MemoryRecord(
            id=_new_ulid(),
            principal_id=payload.principal_id,
            kind=payload.kind.value if isinstance(payload.kind, MemoryKind) else payload.kind,
            status=MemoryStatus.ACTIVE.value,
            content=payload.content,
            provenance=payload.provenance,
            source_execution_id=payload.source_execution_id,
            confidence=payload.confidence,
            expires_at=payload.expires_at,
            sensitivity=payload.sensitivity,
            summary=payload.summary,
        )
        self._session.add(record)
        await self._session.flush()
        log.info(
            "memory.record.created",
            memory_id=record.id,
            principal_id=record.principal_id,
            kind=record.kind,
            provenance=record.provenance,
        )
        return record

    async def get(self, memory_id: str) -> MemoryRecord | None:
        """Return a memory by ID (any status)."""
        return await self._session.get(MemoryRecord, memory_id)

    async def list_active_for_principal(
        self,
        principal_id: str,
        *,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MemoryRecord]:
        """List active memories for a principal.

        Excludes superseded, archived, and forgotten memories.
        Optional filter by kind.
        """
        stmt = (
            select(MemoryRecord)
            .where(
                MemoryRecord.principal_id == principal_id,
                MemoryRecord.status == MemoryStatus.ACTIVE.value,
            )
            .order_by(MemoryRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if kind is not None:
            stmt = stmt.where(MemoryRecord.kind == kind)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def supersede(
        self,
        old_memory_id: str,
        new_memory_id: str,
    ) -> bool:
        """Mark `old_memory_id` as superseded by `new_memory_id`.

        This is the WAX approach to memory conflict: instead of overwriting
        (which loses provenance) or deleting (which loses audit), we mark
        the older record as superseded and link to the newer one. Both
        records are retained; retrieval excludes superseded records by
        default.
        """
        result = await self._session.execute(
            update(MemoryRecord)
            .where(
                MemoryRecord.id == old_memory_id,
                MemoryRecord.status == MemoryStatus.ACTIVE.value,
            )
            .values(
                status=MemoryStatus.SUPERSEDED.value,
                superseded_by=new_memory_id,
                updated_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount > 0:
            log.info(
                "memory.record.superseded",
                old_memory_id=old_memory_id,
                new_memory_id=new_memory_id,
            )
            return True
        return False

    async def forget(self, memory_id: str) -> bool:
        """Mark a memory as forgotten (soft delete).

        The record is retained for audit but excluded from default retrieval.
        """
        result = await self._session.execute(
            update(MemoryRecord)
            .where(
                MemoryRecord.id == memory_id,
                MemoryRecord.status == MemoryStatus.ACTIVE.value,
            )
            .values(
                status=MemoryStatus.FORGOTTEN.value,
                updated_at=datetime.now(timezone.utc),
            )
        )
        if result.rowcount > 0:
            log.info("memory.record.forgotten", memory_id=memory_id)
            return True
        return False

    async def expire_due(self, now: datetime | None = None) -> list[MemoryRecord]:
        """Find active memories whose expires_at has passed.

        Returns them; caller decides whether to forget, archive, or extend.
        This is the entry point for the future "forgetting" worker.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        result = await self._session.execute(
            select(MemoryRecord).where(
                MemoryRecord.status == MemoryStatus.ACTIVE.value,
                MemoryRecord.expires_at.is_not(None),
                MemoryRecord.expires_at <= now,
            )
        )
        return list(result.scalars().all())
