"""
Component ID: CMP_PROVIDER_PYDANTIC_AI_AGENT

capability_load / capability_unload: add or remove a loadable capability for the session.

The tools only forward to handlers injected by the orchestrator, which validates the
request against the capability policy and persists the session's loaded set.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.deps import TurnDeps

logger = structlog.get_logger(__name__)


async def capability_load(
    ctx: RunContext[TurnDeps],
    capability_id: str,
    reason: str,
) -> dict[str, Any]:
    """Load a capability from the "Loadable capabilities" list for this session.

    Use it when the user's request needs a capability listed there. On success the result
    contains the capability's instructions ("prompt", follow them now) and the tools it
    adds ("tools", "mcp_servers"); those tools are callable from your next step in this
    turn, and the capability stays loaded for the rest of the session. Do not load
    capabilities speculatively.

    Args:
        capability_id: Id from the "Loadable capabilities" list.
        reason: Short reason why this request needs the capability.
    """
    handler = ctx.deps.capability_load_handler
    if handler is None:
        return {"status": "unavailable", "reason": "capability loading is not available"}
    result = handler(capability_id, reason)
    logger.info(
        "provider.tool_call.capability_load",
        capability_id=capability_id,
        status=result.get("status"),
    )
    return result


async def capability_unload(
    ctx: RunContext[TurnDeps],
    capability_id: str,
) -> dict[str, Any]:
    """Unload a capability previously added with capability_load.

    Use it when a loaded capability is no longer needed in this conversation; do not
    unload a capability whose tools you used in this turn. Base capabilities of the session
    cannot be unloaded. Its tools stay callable until the end of this turn and are gone
    from the next turn.

    Args:
        capability_id: Id of a capability loaded in this session.
    """
    handler = ctx.deps.capability_unload_handler
    if handler is None:
        return {"status": "unavailable", "reason": "capability loading is not available"}
    result = handler(capability_id)
    logger.info(
        "provider.tool_call.capability_unload",
        capability_id=capability_id,
        status=result.get("status"),
    )
    return result
