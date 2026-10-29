"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Native MCP integration: whole servers connected through pydantic-ai's ``MCPToolset``.
"""

from assistant.extensions.mcp.confirmation import McpConfirmationDenied, check_confirmation
from assistant.extensions.mcp.toolsets import (
    MCP_TOOL_PREFIX,
    McpConnection,
    McpToolsets,
    is_connection_error,
    is_mcp_tool_name,
    mcp_toolsets,
)

__all__ = [
    "MCP_TOOL_PREFIX",
    "McpConfirmationDenied",
    "McpConnection",
    "McpToolsets",
    "check_confirmation",
    "is_connection_error",
    "is_mcp_tool_name",
    "mcp_toolsets",
]
