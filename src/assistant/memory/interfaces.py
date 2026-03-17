"""
Component ID: CMP_MEMORY_FILESYSTEM_STORE

Backend-agnostic memory writer and retrieval interfaces.
"""

from typing import Protocol

from assistant.memory.retrieval.models import RetrievalQuery, RetrievalResult
from assistant.memory.write.models import MemoryUpdateIntent, WriteAudit


class MemoryWriterInterface(Protocol):
    """Protocol for applying memory update intents to a backend."""

    def apply_intent(self, intent: MemoryUpdateIntent, user_id: str | None = None) -> WriteAudit:
        """Apply a memory update intent. Returns audit with status and affected memory_id."""
        ...


class MemoryRetrievalInterface(Protocol):
    """Protocol for retrieving memory artifacts from a backend."""

    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Retrieve relevant memory artifacts with category caps and bounded context."""
        ...

    def ensure_indexes(self) -> None:
        """Build indexes if missing. No-op for backends that manage indexes internally."""
        ...
