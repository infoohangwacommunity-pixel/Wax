"""wax.memory — real memory infrastructure for WAX.

WAX memory is NOT a vector database wrapper. Memory is a semantic /
runtime problem: what to remember, when to retrieve, how to rank, how
to link, how to handle conflicts, how to forget.

Architecture:
- `MemoryRecord` is a single retained fact/observation/event.
- `MemoryKind` discriminates between episodic / semantic / procedural /
  contextual memory.
- `MemoryRepository` handles storage, retrieval ranking (two-stage
  recall + BM25-style blend), typed link traversal, and forgetting.
- Lifecycle (expiry sweeps) runs in `memory.lifecycle`, leader-guarded;
  consolidation is exposed to the intelligence as `memory.consolidate`
  (ADR-0012: the intelligence decides WHEN to consolidate).
- Compression is absent — no module claims it.

INVARIANT (Directive §34): Memory must not equate to "vector database."
We store structured records with provenance, confidence, time, scope.
Vector retrieval is ONE possible retrieval mechanism, not the definition
of memory.
"""

from wax.memory.contracts import (
    MemoryCreate,
    MemoryKind,
    MemoryProvenance,
    MemoryStatus,
)
from wax.memory.models import MemoryRecord
from wax.memory.repository import MemoryRepository

__all__ = [
    "MemoryCreate",
    "MemoryKind",
    "MemoryProvenance",
    "MemoryRecord",
    "MemoryRepository",
    "MemoryStatus",
]
