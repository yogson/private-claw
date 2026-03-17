"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP tool mapping and risk-class models for plugins/mcp/*/tool_map.yaml.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RiskClass(StrEnum):
    """Risk classification for MCP tool policy gates."""

    READONLY = "readonly"
    INTERACTIVE = "interactive"
    SIDE_EFFECTING = "side_effecting"


class McpMappedTool(BaseModel):
    """Single allowlisted MCP tool in a tool mapping."""

    tool_name: str
    summary: str = ""
    input_schema: dict[str, Any] | None = None
    risk_class: RiskClass = RiskClass.READONLY
    requires_confirmation: bool = False


class McpToolMapping(BaseModel):
    """MCP tool mapping (plugins/mcp/*/tool_map.yaml)."""

    server_id: str
    tools: list[McpMappedTool] = Field(default_factory=list)
