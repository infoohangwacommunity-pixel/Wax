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

from collections import Counter
from datetime import UTC, datetime

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
                updated_at=datetime.now(UTC),
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
                updated_at=datetime.now(UTC),
            )
        )
        if result.rowcount > 0:
            log.info("memory.record.forgotten", memory_id=memory_id)
            return True
        return False

    async def expire_due(self, now: datetime | None = None) -> list[MemoryRecord]:
        """Find active memories whose expires_at has passed.

        Returns them; caller decides whether to forget, archive, or extend.
        The runtime lifecycle worker (memory/lifecycle.py) forgets them.
        """
        if now is None:
            now = datetime.now(UTC)

        result = await self._session.execute(
            select(MemoryRecord).where(
                MemoryRecord.status == MemoryStatus.ACTIVE.value,
                MemoryRecord.expires_at.is_not(None),
                MemoryRecord.expires_at <= now,
            )
        )
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Relevance retrieval
    # ------------------------------------------------------------------
    #
    # The bridge used to show the AI only the LAST FIVE EPISODIC memories.
    # That is a telephone-book of recency, not memory: "what did I tell
    # you about my exam?" retrieved whatever happened to be newest. The
    # Foundation PDF (§14) names retrieval, consolidation, and conflict
    # handling as runtime responsibilities.
    #
    # Two-stage retrieval (ADR-0019):
    #
    # 1. RECALL — candidates come from the newest-N pool UNIONed with a
    #    lexical term match (portable ILIKE over summary + serialized
    #    content). This fixes the structural hole where an old but
    #    on-topic memory sat outside the recency pool forever. On
    #    Postgres deployments with the ADR-0019 schema, recall instead
    #    uses the indexed tsvector (GIN) with the same re-ranking stage.
    # 2. RANK — the portable scorer is BM25-style (idf-weighted,
    #    length-normalized term matching over summary + content), then
    #    blended with recency decay and the record's own confidence.
    #    Final ordering happens in Python ON PURPOSE: per-principal
    #    memory counts are small, the computation is portable across
    #    SQLite/Postgres, and the ranking stays inspectable.

    # BM25 parameters (standard Okapi values).
    _BM25_K1 = 1.2
    _BM25_B = 0.75

    @staticmethod
    def _terms(text: str) -> list[str]:
        """Lowercased content terms (>=3 chars, minus a tiny stopword set).

        Kept as a LIST (with duplicates) so term frequency survives for
        BM25; callers that want the vocabulary take set(...).
        """
        stopwords = {
            "the", "and", "for", "with", "that", "this", "you", "your",
            "was", "were", "are", "our", "out", "about", "what", "when",
            "how", "did", "does", "had", "has", "have", "not", "but",
            "all", "can", "will", "would", "could", "should", "from",
            "into", "tell", "said", "say",
        }
        return [
            t for t in
            ("".join(c if c.isalnum() else " " for c in text.lower()).split())
            if len(t) >= 3 and t not in stopwords
        ]

    @classmethod
    def _memory_terms(cls, record: MemoryRecord) -> list[str]:
        """Indexable terms for one record: summary + serialized content."""
        parts = [record.summary or "", str(record.content)]
        text = " ".join(parts)
        return cls._terms(text)

    @classmethod
    def _bm25_scores(
        cls,
        records: list[MemoryRecord],
        query_terms: list[str],
    ) -> list[float]:
        """BM25 term-match scores for the candidate pool.

        Returns one raw BM25 score per record (0.0 when nothing matches).
        IDF uses the standard Okapi formulation, so a term that appears
        in FEW candidate memories contributes far more than a term the
        whole pool shares — the property raw overlap lacked.
        """
        import math

        doc_terms = [cls._memory_terms(r) for r in records]
        doc_counts = [Counter(t) for t in doc_terms]
        n_docs = len(records)
        if n_docs == 0:
            return []
        avg_len = (sum(len(t) for t in doc_terms) / n_docs) or 1.0
        k1, b = cls._BM25_K1, cls._BM25_B

        scores: list[float] = []
        for counts, terms in zip(doc_counts, doc_terms):
            score = 0.0
            seen: set[str] = set()
            for term in query_terms:
                if term in seen:
                    continue
                seen.add(term)
                tf = counts.get(term, 0)
                if tf == 0:
                    continue
                df = sum(1 for c in doc_counts if term in c)
                idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
                score += (
                    idf
                    * (tf * (k1 + 1.0))
                    / (tf + k1 * (1.0 - b + b * (len(terms) / avg_len)))
                )
            scores.append(score)
        return scores

    @classmethod
    def _score(
        cls,
        record: MemoryRecord,
        query_terms: list[str],
        now: datetime,
        bm25_max: float,
        bm25_score: float,
    ) -> float:
        """Final relevance = normalized BM25 × recency/confidence blend.

        - bm25_norm: raw BM25 divided by the pool max — the relative
          lexical strength of this memory among the candidates
        - recency: exp(-age_days / 14) — a relevant old memory still
          beats a coincidentally-worded new one
        - confidence: small boost, honors the record's own confidence field
        """
        if not query_terms or bm25_max <= 0.0 or bm25_score <= 0.0:
            return 0.0
        bm25_norm = bm25_score / bm25_max
        created = record.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_days = max(0.0, (now - created).total_seconds() / 86400.0)
        else:
            age_days = 0.0
        recency = pow(2.718281828, -age_days / 14.0)
        confidence = float(record.confidence) if record.confidence is not None else 0.5
        return bm25_norm * (0.7 + 0.3 * recency) + 0.1 * confidence

    async def _recall_candidates(
        self,
        principal_id: str,
        query_terms: list[str],
        *,
        candidate_pool: int,
        recall_extra: int = 200,
    ) -> list[MemoryRecord]:
        """Stage 1 — candidate RECALL (newest-N ∪ lexical matches).

        Portable across SQLite and Postgres: the lexical arm matches the
        top query terms against summary + serialized content. Terms are
        pre-sanitized (alnum-only by `_terms`), so no LIKE escaping is
        needed. On Postgres with the ADR-0019 schema, callers with
        `use_postgres_fts=True` get the GIN-indexed path instead.
        """
        from sqlalchemy import or_, cast, String as SAString

        base = [
            MemoryRecord.principal_id == principal_id,
            MemoryRecord.status == MemoryStatus.ACTIVE.value,
        ]
        newest = await self._session.execute(
            select(MemoryRecord)
            .where(*base)
            .order_by(MemoryRecord.created_at.desc())
            .limit(candidate_pool)
        )
        records = list(newest.scalars().all())
        seen_ids = {r.id for r in records}

        # Lexical recall arm: take the top few (longest = most selective)
        # terms so the OR-clause stays bounded.
        top_terms = sorted(set(query_terms), key=len, reverse=True)[:8]
        if top_terms:
            likes = []
            for term in top_terms:
                pattern = f"%{term}%"
                likes.append(MemoryRecord.summary.ilike(pattern))
                likes.append(cast(MemoryRecord.content, SAString).ilike(pattern))
            matched = await self._session.execute(
                select(MemoryRecord)
                .where(*base, or_(*likes))
                .order_by(MemoryRecord.created_at.desc())
                .limit(recall_extra)
            )
            for record in matched.scalars().all():
                if record.id not in seen_ids:
                    seen_ids.add(record.id)
                    records.append(record)
        return records

    async def search_relevant(
        self,
        principal_id: str,
        query: str,
        *,
        limit: int = 5,
        candidate_pool: int = 200,
    ) -> list[tuple[MemoryRecord, float]]:
        """Rank a principal's active memories against a query.

        Two-stage retrieval (ADR-0019): portable recall (newest pool ∪
        lexical matches; the Postgres indexed path replaces the lexical
        arm there) followed by BM25-style ranking blended with recency
        and confidence. Returns [(record, score)] descending; zero-score
        records excluded. Considers ALL kinds — evidence lives at every
        layer, and kind filtering is the caller's policy, not the
        storage's.
        """
        query_terms = self._terms(query)
        if not query_terms:
            return []
        candidates = await self._recall_candidates(
            principal_id, query_terms, candidate_pool=candidate_pool
        )
        bm25_scores = self._bm25_scores(candidates, query_terms)
        bm25_max = max(bm25_scores, default=0.0)
        now = datetime.now(UTC)
        scored = [
            (
                record,
                self._score(record, query_terms, now, bm25_max, raw),
            )
            for record, raw in zip(candidates, bm25_scores)
        ]
        scored = [(r, s) for r, s in scored if s > 0.0]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:limit]
