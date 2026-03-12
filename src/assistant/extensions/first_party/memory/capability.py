"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Dedicated memory proposal capability models and validation helpers.
"""

from typing import Any

from assistant.memory.write.models import MemoryUpdateIntent


class MemoryProposalToolCall(MemoryUpdateIntent):
    """Input contract for memory_propose_update capability proposals."""

    requires_user_confirmation: bool = True


def memory_propose_update(arguments: dict[str, Any]) -> MemoryUpdateIntent:
    """Validate and normalize a memory proposal payload to MemoryUpdateIntent."""
    tool_call = MemoryProposalToolCall(**arguments)
    return MemoryUpdateIntent.model_validate(
        tool_call.model_dump(exclude={"requires_user_confirmation"})
    )
