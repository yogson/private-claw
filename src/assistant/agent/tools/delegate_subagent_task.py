"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Tool for enqueueing reusable delegated background tasks.
"""

import os
from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)


async def delegate_subagent_task(
    ctx: RunContext[TurnDeps],
    objective: str,
    model_id: str | None = None,
    directory: str | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    """Create a delegated background task and return immediate acknowledgement.
    Use this when you need to delegate coding work to a background sub-agent (e.g. Claude Code).

    Args:
        objective: Concise task description for the sub-agent. Be specific about what to accomplish.
        model_id: Optional model override (e.g. "claude-sonnet-4"). Omit to use the default model.
        directory: Optional workspace path for the sub-agent. Must be an existing
            directory; if invalid, the task is rejected. MUST be specified for Claude Code.
        backend: Optional backend selector. Use "claude_code" for the standard Claude Code
            backend or "claude_code_streaming" for the streaming backend. Omit to use the
            default backend.
    """
    handler = ctx.deps.delegation_enqueue_handler
    if handler is None:
        return {
            "accepted": False,
            "status": "unavailable",
            "rejection_reason": "delegation disabled",
        }
    backend_params: dict[str, Any] = {}
    if directory:
        if not os.path.isdir(directory):
            return {
                "accepted": False,
                "status": "error",
                "rejection_reason": f"directory does not exist: {directory}",
            }
        backend_params["directory"] = directory
    request: dict[str, Any] = {
        "objective": objective,
        "model_id": model_id,
        "tool_params": ctx.deps.tool_runtime_params.get("delegate_subagent_task", {}),
        "backend_params": backend_params,
        "backend": backend,
    }
    result = await handler(request)
    logger.info(
        "provider.tool_call.delegate_subagent_task",
        accepted=bool(result.get("accepted")),
        status=result.get("status"),
        task_id=result.get("task_id"),
    )
    return result
