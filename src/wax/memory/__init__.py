"""wax.memory — real memory infrastructure for WAX.

WAX memory is NOT a vector database wrapper. Memory is a semantic /
runtime problem: what to remember, when to retrieve, how to compress,
how to handle conflicts, how to forget.

Architecture:
- `MemoryRecord` is a single retained fact/observation/event.
- `MemoryKind` discriminates between episodic / semantic / procedural /
  contextual memory.
- `MemoryRepository` handles storage and retrieval.
- A future `MemoryService` will handle consolidation, retrieval ranking,
  compression, forgetting — but the storage primitive comes first.

INVARIANT (Directive §34): Memory must not equate to "vector database."
We store structured records with provenance, confidence, time, scope.
Vector retrieval is ONE possible retrieval mechanism, not the definition
of memory.
"""

from wax.memory.contracts import MemoryKind, MemoryStatus, MemoryProvenance, MemoryCreate, MemoryRead
from wax.memory.models import MemoryRecord
from wax.memory.repository import MemoryRepository

__all__ = [
    "MemoryRecord",
    "MemoryKind",
    "MemoryStatus",
    "MemoryProvenance",
    "MemoryCreate",
    "MemoryRead",
    "MemoryRepository",
]
