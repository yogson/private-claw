"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Memory capability models, proposal validation utilities and the ``memory_update``
confirmation kind.
"""

from assistant.extensions.first_party.memory.capability import (
    MemoryProposalToolCall,
    canonicalize_memory_args,
    memory_propose_update,
    normalize_candidate_for_upsert,
)
from assistant.extensions.first_party.memory.confirmation import (
    MEMORY_BODY_MAX_CHARS,
    MEMORY_UPDATE_KIND,
    MemoryUpdateHandler,
    build_memory_update_draft,
    memory_body_length_error,
)

__all__ = [
    "MEMORY_BODY_MAX_CHARS",
    "MEMORY_UPDATE_KIND",
    "MemoryUpdateHandler",
    "MemoryProposalToolCall",
    "build_memory_update_draft",
    "canonicalize_memory_args",
    "memory_body_length_error",
    "memory_propose_update",
    "normalize_candidate_for_upsert",
]
