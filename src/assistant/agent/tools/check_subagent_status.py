"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Tool for polling the status of a previously delegated background task.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import (
    MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN,
    TurnDeps,
    consume_subagent_poll_budget,
)

logger = structlog.get_logger(__name__)


async def check_subagent_status(
    ctx: RunContext[TurnDeps],
    task_id: str,
) -> dict[str, Any]:
    """Check the current status of a delegated background task.

    You are automatically notified in a new turn once a delegated task
    completes - do NOT call this repeatedly to poll for completion, and never
    call it back-to-back for the same task_id in one turn. Use it only when:
    the user explicitly asks for a progress update on a task that's still
    running, or you need to confirm a task's final status/result before
    reporting to the user. The response always includes "recent_log" - the
    last 10 lines of the task's activity log - so you rarely need a separate
    read_subagent_log call. If completed, the response includes the result
    (summary text and usage); if failed, it includes the error. If still
    pending/running, the response includes a "hint" telling you to end your
    turn instead of checking again. This tool is rate-limited per task per
    turn - repeated calls will be refused.

    Args:
        task_id: The task_id returned by delegate_subagent_task.
    """
    if not consume_subagent_poll_budget(ctx.deps, task_id):
        return {
            "found": False,
            "status": "poll_limit_reached",
            "error": (
                f"Already checked task {task_id} {MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN} times "
                "this turn. Stop checking and end your turn now - you will be notified "
                "automatically when it completes."
            ),
        }
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
