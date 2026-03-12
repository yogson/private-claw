"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Memory capability models and proposal validation utilities.
"""

from assistant.extensions.first_party.memory.capability import (
    MemoryProposalToolCall,
    memory_propose_update,
)

__all__ = [
    "MemoryProposalToolCall",
    "memory_propose_update",
]
