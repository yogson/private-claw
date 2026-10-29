"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

"Notify before call" hook for MCP tools listed in a server's ``confirm_tools`` and for tools
annotated ``destructiveHint: true`` (see ``toolsets.McpConfirmationToolset``).

This is NOT an approval gate today. It does not block the call:
1. Tool is invoked by the LLM.
2. check_confirmation() hands a "confirmation required" payload to tool_call_notifier, if
   one is set (verbose mode). The notifier only posts a message to the chat.
3. The call then proceeds, whether or not a notifier exists.

McpConfirmationDenied is honoured when a ``confirm`` callable raises it (the call is skipped
and the model gets a tool error), but nothing raises it yet. A real approval flow (pause,
ask the user, resume) is future work.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext

from assistant.agent.deps import TurnDeps

logger = structlog.get_logger(__name__)


class McpConfirmationDenied(Exception):
    """Raised when user denies confirmation for an MCP tool call."""

    def __init__(self, server_id: str, tool_name: str) -> None:
        self.server_id = server_id
        self.tool_name = tool_name
        super().__init__(
            f"MCP tool '{tool_name}' (server={server_id}) requires confirmation but it was denied."
        )


async def check_confirmation(
    ctx: RunContext[TurnDeps],
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> None:
    """Notify before an MCP tool call that is marked as sensitive; does not block it.

    If a tool_call_notifier is available on TurnDeps (verbose mode), it is called with a
    JSON-encoded "confirmation required" payload so the user can see the call. The notifier
    only posts a message; it does not wait for or collect an answer, and the call proceeds
    afterwards. With no notifier the call proceeds silently, which also keeps non-interactive
    contexts (e.g. scheduled tasks) running.

    A future approval flow would raise McpConfirmationDenied from here to veto the call.
    """
    import json

    # Same name the model sees (mcp_<server>_<tool>); mirrors the verbose tool-call message.
    model_tool_name = f"mcp_{server_id}_{tool_name}"
    notifier = ctx.deps.tool_call_notifier
    if notifier is None:
        logger.debug(
            "mcp.confirmation.no_notifier",
            server_id=server_id,
            tool_name=tool_name,
            hint="No tool_call_notifier configured; proceeding without notifying",
        )
        return

    payload = json.dumps(
        {
            "type": "mcp_confirmation_required",
            "tool": model_tool_name,
            "server_id": server_id,
            "tool_name": tool_name,
            "arguments": arguments or {},
        },
        default=str,
    )
    logger.info(
        "mcp.confirmation.requested",
        server_id=server_id,
        tool_name=tool_name,
    )
    await notifier(model_tool_name, payload)
