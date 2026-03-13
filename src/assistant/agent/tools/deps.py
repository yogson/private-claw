"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Dependencies injected into agent tools for turn execution.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

MAX_MEMORY_WRITES_PER_TURN = 3


@dataclass
class TurnDeps:
    """Dependencies injected into agent tools for turn execution."""

    writes_approved: list[None]  # mutable: append when we approve a write
    seen_intent_ids: set[str]  # mutable: deduplicate intent_id per turn
    memory_search_handler: Callable[[str, int, list[str] | None], dict[str, Any]] | None = None
