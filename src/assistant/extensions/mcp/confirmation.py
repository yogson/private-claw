"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP tool confirmation gate for tools listed in a server's ``confirm_tools`` and for tools
annotated ``destructiveHint: true`` (see ``toolsets.McpConfirmationToolset``).

The confirmation flow:
1. Tool is invoked by the LLM.
2. check_confirmation() fires tool_call_notifier (if set) with a confirmation request.
3. If no notifier is configured, the tool proceeds (fail-open for non-interactive contexts).
4. When notifier is present: it is responsible for user-facing confirmation and raises
   McpConfirmationDenied if the user declines.
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
            f"MCP tool '{tool_name}' (server={server_id}) requires confirmation "
            f"but was denied or no confirmation handler is available."
        )


async def check_confirmation(
    ctx: RunContext[TurnDeps],
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> None:
    """Enforce confirmation for an MCP tool call.

    If a tool_call_notifier is available on TurnDeps, it is called with
    a JSON-encoded confirmation payload. The notifier is expected to raise
    McpConfirmationDenied if the user declines.

    If no notifier is configured, the call proceeds (fail-open). This allows
    non-interactive contexts (e.g. scheduled tasks) to run without blocking.
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
            hint="No tool_call_notifier configured; proceeding without confirmation",
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
