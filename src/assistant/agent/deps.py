"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Dependencies injected into agent tools for turn execution.
Placed at agent level to avoid circular imports with extensions (e.g. MCP bridge).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

MAX_MEMORY_WRITES_PER_TURN = 3
# Hard cap on check_subagent_status/read_subagent_log calls for the same
# task_id within one turn (the two tools share this budget). Prompt guidance
# alone ("don't poll in a loop") is not reliable enough on its own: a model
# babysitting a slow delegated task has been observed polling back-to-back
# until it hit pydantic-ai's own request_limit and crashed the whole turn
# with UsageLimitExceeded. This cap forces a hard stop well before that.
MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN = 2


@dataclass
class TurnDeps:
    """Dependencies injected into agent tools for turn execution."""

    writes_approved: list[None]  # mutable: append when we approve a write
    seen_intent_ids: set[str]  # mutable: deduplicate intent_id per turn
    memory_search_handler: Callable[[str, int, list[str] | None], dict[str, Any]] | None = None
    delegation_enqueue_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    delegation_status_handler: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    """Optional async callback: task_id -> status dict for a delegated task."""
    delegation_cancel_handler: Callable[[str], Awaitable[dict[str, Any]]] | None = None
    """Optional async callback: task_id -> result dict after cancelling a delegated task."""
    delegation_log_handler: Callable[[str, int], Awaitable[dict[str, Any]]] | None = None
    """Optional async callback: (task_id, tail_lines) -> activity log tail for a delegated task."""
    subagent_poll_counts: dict[str, int] = field(default_factory=dict)
    """Mutable: task_id -> number of check_subagent_status/read_subagent_log calls this turn.

    Shared budget between both tools, capped at MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN,
    to stop a model from polling a slow delegated task in a tight loop.
    """
    tool_runtime_params: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # per-tool merged params from tools.yaml + capability overrides
    tool_call_notifier: Callable[[str, str], Awaitable[None]] | None = None
    """Optional async callback fired before each tool call: (tool_name, args_json) -> None."""
    streaming_text_notifier: Callable[[str], Awaitable[None]] | None = None
    """Optional async callback fired with text content generated alongside a tool call.

    Called immediately when the model produces a mixed response (text + tool calls),
    before the tools run.  When set, intermediate texts are streamed in real-time and
    excluded from the final response_text returned by run_turn.
    """
    user_id: str | None = None
    """User ID for the current turn (injected from orchestrator)."""
    vocabulary_store: Any | None = None
    """VocabularyStore instance for language learning tools (Any to avoid circular import)."""


def consume_subagent_poll_budget(deps: TurnDeps, task_id: str) -> bool:
    """Record one check_subagent_status/read_subagent_log call for task_id.

    Returns True if this call is within budget, False if the caller has
    already exhausted MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN checks on this
    task_id this turn and should be refused instead of hitting the handler.
    """
    count = deps.subagent_poll_counts.get(task_id, 0) + 1
    deps.subagent_poll_counts[task_id] = count
    return count <= MAX_SUBAGENT_POLLS_PER_TASK_PER_TURN
