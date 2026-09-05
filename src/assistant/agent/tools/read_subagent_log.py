"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Tool for peeking at a delegated background task's live activity log.
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


async def read_subagent_log(
    ctx: RunContext[TurnDeps],
    task_id: str,
    tail_lines: int = 200,
) -> dict[str, Any]:
    """Read the recent activity log of a delegated background task.

    Shows what a delegated task has actually been doing (assistant messages,
    tool calls, tool results), not just its overall status. Works for both
    the "claude_code" and "claude_code_streaming" backends.

    This is diagnostic, not a progress bar: call it at most once per explicit
    request, never repeatedly in a loop to watch a task run. You are
    automatically notified in a new turn once a task completes. Use this only
    when the user explicitly asks what a running task is doing, or to
    investigate after a task failed/timed out. This tool shares a per-task,
    per-turn rate limit with check_subagent_status - repeated calls will be
    refused.

    Args:
        task_id: The task_id returned by delegate_subagent_task.
        tail_lines: Max number of most-recent log lines to return (default 200).
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
    handler = ctx.deps.delegation_log_handler
    if handler is None:
        return {
            "found": False,
            "status": "unavailable",
            "rejection_reason": "delegation disabled",
        }
    result = await handler(task_id, tail_lines)
    logger.info(
        "provider.tool_call.read_subagent_log",
        task_id=task_id,
        found=bool(result.get("found")),
        line_count=len(result.get("lines", [])),
    )
    return result
