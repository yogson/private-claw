"""
Component ID: CMP_AGENT_SUBAGENT_COORDINATOR

Tool for enqueueing reusable delegated background tasks.
"""

import os
from typing import Any, Literal

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
    plugin_dirs: list[str] | None = None,
    workspace_mode: Literal["worktree", "readonly", "in_place"] | None = None,
    branch: str | None = None,
    base: str | None = None,
) -> dict[str, Any]:
    """Create a delegated background task and return immediate acknowledgement.
    Use this when you need to delegate coding work to a background sub-agent (e.g. Claude Code).

    An accepted result includes ``workspace`` (mode, directory, branch, worktree, base,
    worktree_created): where the task actually runs. The same object is in the completion
    notification and in check_subagent_status.

    Args:
        objective: Concise task description for the sub-agent. Be specific about what to accomplish.
        model_id: Optional model override (e.g. "claude-sonnet-4"). Omit to use the default model.
        directory: Optional workspace path for the sub-agent. Must be an existing
            directory; if invalid, the task is rejected. MUST be specified for Claude Code.
            For a git repository pass the repo (e.g. its main checkout); where the sub-agent
            actually runs depends on workspace_mode.
        backend: Optional backend selector. Use "claude_code" for fire-and-forget one shot
            simple task or "claude_code_streaming" for complex tasks with feedback loop.
            Omit to use the default backend - "claude_code".
        plugin_dirs: Optional list of local Claude Code plugin directories to load for this
            run only (e.g. a checked-out plugin providing extra Skills). Each entry must be
            an existing directory; if invalid, the task is rejected.
        workspace_mode: How the sub-agent uses ``directory``. Omit for the default:
            "worktree" for a git repository, "in_place" for any other directory. Passing
            "worktree" explicitly is unnecessary for a git repository and fails for a
            non-git directory.
            - "worktree": the coordinator runs the task in its own git worktree of the
              repository for ``branch``, created from a freshly fetched ``base``. Later
              tasks on the same branch (review, fix rounds) reuse that worktree with its
              state. A worktree has one running task at a time: a second task on the same
              branch is rejected, so use different branches for parallel work.
            - "readonly": run in ``directory`` itself for analysis/review without changes.
            - "in_place": run in ``directory`` itself and change it there, under an
              exclusive per-checkout lock (tasks wait for each other). Only for work that
              must happen in that checkout, e.g. fast-forwarding the main checkout after
              a merge or editing gitignored local config.
        branch: Optional, worktree mode only. Branch to work on: an existing local or
            remote branch is checked out, a new one is created from ``base``. If omitted,
            a new branch "subagent/<task_id>" is created and returned in
            ``workspace.branch``. For follow-up rounds of the same work (review, fixes)
            pass that returned ``workspace.branch`` as ``branch`` to continue in the same
            worktree.
        base: Worktree mode only, for a new branch. Omit for the repo default
            (origin/master or the remote's default branch).
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
    if plugin_dirs:
        for path in plugin_dirs:
            if not os.path.isdir(path):
                return {
                    "accepted": False,
                    "status": "error",
                    "rejection_reason": f"plugin_dirs entry does not exist: {path}",
                }
        backend_params["plugin_dirs"] = list(plugin_dirs)
    workspace = {
        key: value
        for key, value in (("mode", workspace_mode), ("branch", branch), ("base", base))
        if isinstance(value, str) and value.strip()
    }
    request: dict[str, Any] = {
        "objective": objective,
        "model_id": model_id,
        "tool_params": ctx.deps.tool_runtime_params.get("delegate_subagent_task", {}),
        "backend_params": backend_params,
        "backend": backend,
    }
    if workspace:
        request["workspace"] = workspace
    result = await handler(request)
    logger.info(
        "provider.tool_call.delegate_subagent_task",
        accepted=bool(result.get("accepted")),
        status=result.get("status"),
        task_id=result.get("task_id"),
    )
    return result
