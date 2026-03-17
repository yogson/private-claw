"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP bridge: server discovery, tool mapping, risk-class policy, normalized invocation.
"""

from assistant.extensions.mcp.bridge import McpBridge
from assistant.extensions.mcp.loader import capability_id_for_mcp_tool, load_tool_mappings
from assistant.extensions.mcp.models import McpMappedTool, McpToolMapping, RiskClass

__all__ = [
    "McpBridge",
    "McpMappedTool",
    "McpToolMapping",
    "RiskClass",
    "capability_id_for_mcp_tool",
    "load_tool_mappings",
]
