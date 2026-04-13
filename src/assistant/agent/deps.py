"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Dependencies injected into agent tools for turn execution.
Placed at agent level to avoid circular imports with extensions (e.g. MCP bridge).
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

MAX_MEMORY_WRITES_PER_TURN = 3


@dataclass
class TurnDeps:
    """Dependencies injected into agent tools for turn execution."""

    writes_approved: list[None]  # mutable: append when we approve a write
    seen_intent_ids: set[str]  # mutable: deduplicate intent_id per turn
    memory_search_handler: Callable[[str, int, list[str] | None], dict[str, Any]] | None = None
    delegation_enqueue_handler: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
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
