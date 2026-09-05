"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Tool for polling the status of a previously delegated background task.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)


async def check_subagent_status(
    ctx: RunContext[TurnDeps],
    task_id: str,
) -> dict[str, Any]:
    """Check the current status of a delegated background task.

    Use this to find out whether a task previously started with
    delegate_subagent_task is still pending/running, or has already
    completed/failed - instead of only finding out via a timeout or waiting
    for a completion notification. If completed, the response includes the
    result (summary text and usage); if failed, it includes the error.

    Args:
        task_id: The task_id returned by delegate_subagent_task.
    """
    handler = ctx.deps.delegation_status_handler
    if handler is None:
        return {
            "found": False,
            "status": "unavailable",
            "rejection_reason": "delegation disabled",
        }
    result = await handler(task_id)
    logger.info(
        "provider.tool_call.check_subagent_status",
        task_id=task_id,
        found=bool(result.get("found")),
        status=result.get("status"),
    )
    return result
