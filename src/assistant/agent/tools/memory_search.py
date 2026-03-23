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
    """Retrieve compact memory context on demand.

    The orchestrator caps each match body length; logs here keep a short preview only.
    """
    bounded_limit = max(1, min(limit, 5))
    logger.info(
        "provider.tool_call.memory_search",
        phase="entry",
        query=query,
        limit=bounded_limit,
        memory_types=memory_types or [],
    )
    handler = ctx.deps.memory_search_handler
    if handler is None:
        result = {
            "status": "unavailable",
            "reason": "memory retrieval unavailable",
            "matches": [],
        }
        logger.info(
            "provider.tool_result.memory_search",
            status=result["status"],
            reason=result["reason"],
            match_count=0,
        )
        return result
    try:
        result = handler(query, bounded_limit, memory_types)
        matches = result.get("matches", []) if isinstance(result, dict) else []
        match_count = len(matches)
        match_bodies: list[str] = []
        for m in matches:
            if isinstance(m, dict):
                body = m.get("body", "")
                body_str = body if isinstance(body, str) else str(body)
                match_bodies.append(body_str[:500] + ("..." if len(body_str) > 500 else ""))
        logger.info(
            "provider.tool_result.memory_search",
            status=result.get("status", "ok"),
            match_count=match_count,
            matches=match_bodies,
        )
        return result
    except Exception as exc:
        result = {"status": "failed", "reason": str(exc), "matches": []}
        logger.warning(
            "provider.tool_result.memory_search",
            status=result["status"],
            reason=result["reason"],
            error=str(exc),
        )
        return result
