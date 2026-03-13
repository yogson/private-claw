"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Memory capability models and proposal validation utilities.
"""

from assistant.extensions.first_party.memory.capability import (
    MemoryProposalToolCall,
    canonicalize_memory_args,
    memory_propose_update,
    normalize_candidate_for_upsert,
)

__all__ = [
    "MemoryProposalToolCall",
    "canonicalize_memory_args",
    "memory_propose_update",
    "normalize_candidate_for_upsert",
]
