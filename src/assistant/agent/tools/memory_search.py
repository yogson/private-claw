"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

Memory search tool for Pydantic AI agent.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.tools.deps import TurnDeps

logger = structlog.get_logger(__name__)


def memory_search(
    ctx: RunContext[TurnDeps],
    query: str,
    limit: int = 3,
    memory_types: list[str] | None = None,
) -> dict[str, Any]:
    """Retrieve compact memory context on demand."""
    handler = ctx.deps.memory_search_handler
    bounded_limit = max(1, min(limit, 5))
    if handler is None:
        logger.info(
            "provider.tool_call.memory_search",
            status="unavailable",
            query=query,
            limit=bounded_limit,
            memory_types=memory_types or [],
        )
        return {
            "status": "unavailable",
            "reason": "memory retrieval unavailable",
            "matches": [],
        }
    try:
        result = handler(query, bounded_limit, memory_types)
        logger.info(
            "provider.tool_call.memory_search",
            status=result.get("status", "ok"),
            query=query,
            limit=bounded_limit,
            memory_types=memory_types or [],
            match_count=len(result.get("matches", [])) if isinstance(result, dict) else 0,
        )
        return result
    except Exception as exc:
        logger.warning(
            "provider.tool_call.memory_search",
            status="failed",
            query=query,
            limit=bounded_limit,
            memory_types=memory_types or [],
            error=str(exc),
        )
        return {"status": "failed", "reason": str(exc), "matches": []}
