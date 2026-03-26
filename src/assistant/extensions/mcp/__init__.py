"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP bridge: server discovery, tool mapping, risk-class policy, normalized invocation.
"""

from assistant.extensions.mcp.bridge import McpBridge
from assistant.extensions.mcp.confirmation import McpConfirmationDenied, check_confirmation
from assistant.extensions.mcp.loader import capability_id_for_mcp_tool, load_tool_mappings
from assistant.extensions.mcp.models import McpMappedTool, McpToolMapping, RiskClass
from assistant.extensions.mcp.session_pool import McpSessionPool

__all__ = [
    "McpBridge",
    "McpConfirmationDenied",
    "McpMappedTool",
    "McpSessionPool",
    "McpToolMapping",
    "RiskClass",
    "capability_id_for_mcp_tool",
    "check_confirmation",
    "load_tool_mappings",
]
