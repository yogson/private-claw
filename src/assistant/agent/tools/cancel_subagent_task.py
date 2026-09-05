"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Tool for cancelling a previously delegated background task.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)


async def cancel_subagent_task(
    ctx: RunContext[TurnDeps],
    task_id: str,
) -> dict[str, Any]:
    """Cancel a delegated background task that is still pending or running.

    A task not yet started is pre-empted before it runs. A task already
    running has its sub-process/sub-query terminated - this actually stops
    the work, it does not just relabel the record. Calling this on a task
    that already finished (completed/failed) is a no-op that returns its
    final status.

    Args:
        task_id: The task_id returned by delegate_subagent_task.
    """
    handler = ctx.deps.delegation_cancel_handler
    if handler is None:
        return {
            "found": False,
            "status": "unavailable",
            "rejection_reason": "delegation disabled",
        }
    result = await handler(task_id)
    logger.info(
        "provider.tool_call.cancel_subagent_task",
        task_id=task_id,
        found=bool(result.get("found")),
        status=result.get("status"),
    )
    return result
