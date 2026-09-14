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
from wax.state.memory_models import MemoryLinkRecord, MemoryRecord

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
            importance=payload.importance,
            observed_at=payload.observed_at,
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

        Excludes superseded and forgotten memories.
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

    # --- typed memory links (ADR-0022, mission Phase 3) --------------------

    async def link(
        self,
        from_memory_id: str,
        to_memory_id: str,
        kind: str,
        *,
        execution_id: str | None = None,
    ) -> MemoryLinkRecord | None:
        """Create (or return the existing) typed edge between two memories.

        Rules: both endpoints must be ACTIVE memories of the SAME
        principal; a memory cannot link to itself; the edge is idempotent
        (re-linking the same pair+kind returns the existing row). Returns
        None when ownership/state validation fails — callers decide how
        honestly to surface that.
        """
        from wax.memory.contracts import MemoryLinkKind

        if isinstance(kind, MemoryLinkKind):
            kind = kind.value
        if kind not in {k.value for k in MemoryLinkKind}:
            log.warning("memory.link.invalid_kind", kind=kind)
            return None
        if from_memory_id == to_memory_id:
            log.warning("memory.link.self_link_denied", memory_id=from_memory_id)
            return None

        source = await self.get(from_memory_id)
        target = await self.get(to_memory_id)
        if (
            source is None
            or target is None
            or source.status != MemoryStatus.ACTIVE.value
            or target.status != MemoryStatus.ACTIVE.value
            or source.principal_id != target.principal_id
        ):
            log.warning(
                "memory.link.validation_failed",
                from_memory_id=from_memory_id,
                to_memory_id=to_memory_id,
            )
            return None

        existing = await self._session.execute(
            select(MemoryLinkRecord).where(
                MemoryLinkRecord.from_memory_id == from_memory_id,
                MemoryLinkRecord.to_memory_id == to_memory_id,
                MemoryLinkRecord.kind == kind,
            )
        )
        found = existing.scalars().first()
        if found is not None:
            return found

        edge = MemoryLinkRecord(
            id=_new_ulid(),
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            kind=kind,
            principal_id=source.principal_id,
            created_by_execution_id=execution_id,
        )
        self._session.add(edge)
        await self._session.flush()
        log.info(
            "memory.link.created",
            link_id=edge.id,
            from_memory_id=from_memory_id,
            to_memory_id=to_memory_id,
            kind=kind,
        )
        return edge

    async def unlink(
        self, from_memory_id: str, to_memory_id: str, kind: str
    ) -> bool:
        """Remove a typed edge. Returns True when a row was deleted.

        OWNERSHIP (defense in depth): links are principal-scoped through
        their endpoints — the edge is deleted only when BOTH endpoint
        memories exist and belong to the SAME principal. A missing or
        cross-principal pair refuses loudly instead of deleting. Callers
        (the capability layer) still enforce WHOSE memories these are;
        this makes the repo-level primitive itself incapable of touching
        another principal's graph.
        """
        from sqlalchemy import delete

        from wax.memory.contracts import MemoryLinkKind

        if isinstance(kind, MemoryLinkKind):
            kind = kind.value
        endpoints = await self._session.execute(
            select(MemoryRecord.principal_id).where(
                MemoryRecord.id.in_([from_memory_id, to_memory_id])
            )
        )
        owners = {principal for (principal,) in endpoints.all()}
        if len(owners) != 1:
            # Missing endpoint(s) or a cross-principal pair: refuse.
            log.warning(
                "memory.link.unlink_refused",
                from_memory_id=from_memory_id,
                to_memory_id=to_memory_id,
                owners=len(owners),
            )
            return False
        result = await self._session.execute(
            delete(MemoryLinkRecord).where(
                MemoryLinkRecord.from_memory_id == from_memory_id,
                MemoryLinkRecord.to_memory_id == to_memory_id,
                MemoryLinkRecord.kind == kind,
            )
        )
        if result.rowcount > 0:
            await self._session.flush()
            log.info(
                "memory.link.removed",
                from_memory_id=from_memory_id,
                to_memory_id=to_memory_id,
                kind=kind,
            )
            return True
        return False

    async def links_for(
        self, memory_id: str, *, kind: str | None = None
    ) -> list[tuple[MemoryLinkRecord, str]]:
        """All edges touching a memory, either direction.

        Returns [(edge, direction)] with direction "outgoing" | "incoming".
        """
        from wax.memory.contracts import MemoryLinkKind

        kind_value = kind.value if isinstance(kind, MemoryLinkKind) else kind
        outgoing = await self._session.execute(
            select(MemoryLinkRecord).where(
                MemoryLinkRecord.from_memory_id == memory_id,
                *([MemoryLinkRecord.kind == kind_value] if kind_value else []),
            )
        )
        incoming = await self._session.execute(
            select(MemoryLinkRecord).where(
                MemoryLinkRecord.to_memory_id == memory_id,
                *([MemoryLinkRecord.kind == kind_value] if kind_value else []),
            )
        )
        edges = [
            (edge, "outgoing") for edge in outgoing.scalars().all()
        ] + [(edge, "incoming") for edge in incoming.scalars().all()]
        return edges

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

    # Linked-memory expansion bound: at most this many one-hop neighbors
    # join the evidence set per top-scoring anchor (ADR-0022).
    _LINKED_PER_ANCHOR = 5

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

        def _hits(counts: Counter, term: str) -> int:
            """Occurrences of `term` in one document, prefix-aware.

            The recall arm matches SUBSTRINGS (ilike %term%), so ranking
            must agree or recalled candidates score zero and silently
            vanish (the evaluation suite caught exactly that: query
            'study' recalled 'studying', then ranked it out). A query
            term matches document tokens that ARE it or START with it —
            lightweight stemming, no dependencies.
            """
            return sum(
                count
                for token, count in counts.items()
                if token == term or token.startswith(term)
            )

        scores: list[float] = []
        for counts, terms in zip(doc_counts, doc_terms):
            score = 0.0
            seen: set[str] = set()
            for term in query_terms:
                if term in seen:
                    continue
                seen.add(term)
                tf = _hits(counts, term)
                if tf == 0:
                    continue
                df = sum(1 for c in doc_counts if _hits(c, term) > 0)
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
        """Final relevance = normalized BM25 × recency/confidence blend
        + importance weight (ADR-0022, mission §6.3/§75).

        - bm25_norm: raw BM25 divided by the pool max — the relative
          lexical strength of this memory among the candidates
        - recency: exp(-age_days / 14) — a relevant old memory still
          beats a coincidentally-worded new one
        - confidence: small boost, honors the record's own confidence field
        - importance: small boost for what the intelligence explicitly
          marked as mattering (NULL = neutral 0.5); it can lift or sink
          a memory but cannot fabricate relevance (multiplied by bm25_norm)
        """
        if not query_terms or bm25_max <= 0.0 or bm25_score <= 0.0:
            return 0.0
        bm25_norm = bm25_score / bm25_max
        created = record.observed_at or record.created_at
        if created is not None:
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age_days = max(0.0, (now - created).total_seconds() / 86400.0)
        else:
            age_days = 0.0
        recency = pow(2.718281828, -age_days / 14.0)
        confidence = float(record.confidence) if record.confidence is not None else 0.5
        importance = (
            float(record.importance) if record.importance is not None else 0.5
        )
        return (
            bm25_norm * (0.7 + 0.3 * recency)
            + 0.1 * confidence
            + 0.1 * (importance - 0.5) * bm25_norm
        )

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
        from sqlalchemy import String as SAString
        from sqlalchemy import cast, or_

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
        results = scored[:limit]

        # Linked-memory expansion (ADR-0022, mission Phase 3/§49): the
        # ACTIVE one-hop neighbors of a hit join the evidence set with a
        # DAMPED score — if this memory matters for the query, the memory
        # it supports or contradicts plausibly matters too. Bounded per
        # anchor; never displaces directly-relevant records because the
        # damped score sorts below the anchor.
        if results:
            expansions: list[tuple[MemoryRecord, float]] = []
            seen = {r.id for r, _ in results}
            anchors = [r for r, _ in results[:3]]
            for anchor in anchors:
                edges = await self.links_for(anchor.id)
                neighbor_ids = [
                    (
                        e.to_memory_id
                        if e.from_memory_id == anchor.id
                        else e.from_memory_id
                    )
                    for e, _ in edges
                ]
                for nid in neighbor_ids[: self._LINKED_PER_ANCHOR]:
                    if nid in seen:
                        continue
                    neighbor = await self.get(nid)
                    if (
                        neighbor is None
                        or neighbor.status != MemoryStatus.ACTIVE.value
                        or neighbor.principal_id != principal_id
                    ):
                        continue
                    seen.add(nid)
                    expansions.append((neighbor, results[0][1] * 0.6))
            if expansions:
                results = sorted(results + expansions, key=lambda p: p[1], reverse=True)
                results = results[:limit]
        return results
