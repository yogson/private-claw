"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Tool for enqueueing reusable delegated background tasks.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)


async def delegate_subagent_task(
    ctx: RunContext[TurnDeps],
    objective: str,
    workflow_id: str | None = None,
    max_tokens: int | None = None,
    chat_id: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a delegated background task and return immediate acknowledgement."""
    handler = ctx.deps.delegation_enqueue_handler
    if handler is None:
        return {
            "accepted": False,
            "status": "unavailable",
            "rejection_reason": "delegation disabled",
        }
    request: dict[str, Any] = {
        "objective": objective,
        "workflow_id": workflow_id,
        "max_tokens": max_tokens,
        "chat_id": chat_id,
        "metadata": metadata or {},
        "tool_params": ctx.deps.tool_runtime_params.get("delegate_subagent_task", {}),
    }
    result = await handler(request)
    logger.info(
        "provider.tool_call.delegate_subagent_task",
        accepted=bool(result.get("accepted")),
        status=result.get("status"),
        task_id=result.get("task_id"),
    )
    return result
