"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Re-exports turn dependencies from agent.deps for backward compatibility.
"""

from assistant.agent.deps import (
    MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN,
    TurnDeps,
    consume_subagent_poll_budget,
)

__all__ = [
    "MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN",
    "TurnDeps",
    "consume_subagent_poll_budget",
]
