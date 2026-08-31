"""
Component ID: CMP_CORE_CAPABILITIES

Capability definition loader and schemas for config/capabilities/*.yaml.
"""

from assistant.core.capabilities.loader import (
    collect_claude_code_mcp_servers,
    expand_nested_capabilities,
    load_capability_definitions,
)
from assistant.core.capabilities.schemas import (
    CapabilityDefinition,
    CapabilityToolBinding,
    CapabilityToolOverride,
)

__all__ = [
    "CapabilityDefinition",
    "CapabilityToolBinding",
    "CapabilityToolOverride",
    "collect_claude_code_mcp_servers",
    "expand_nested_capabilities",
    "load_capability_definitions",
]
