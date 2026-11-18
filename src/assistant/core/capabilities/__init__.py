"""
Component ID: CMP_CORE_CAPABILITIES

Capability manifests (config/capabilities/*.yaml): schemas, parser, the versioned
CapabilityCatalog with live reload, and the shared active-set resolver.
"""

from assistant.core.capabilities.catalog import CapabilityCatalog
from assistant.core.capabilities.claude_code_settings import (
    ClaudeCodeSettingsSync,
    SubagentMcpServersSync,
    apply_claude_code_settings,
    collect_claude_code_mcp_servers,
)
from assistant.core.capabilities.interfaces import CapabilityCatalogInterface
from assistant.core.capabilities.loader import (
    CapabilityLoadError,
    collect_mcp_server_ids,
    load_capability_definitions,
)
from assistant.core.capabilities.resolver import (
    expand_nested_capabilities,
    resolve_active_capabilities,
)
from assistant.core.capabilities.schemas import (
    CapabilityDefinition,
    CapabilityToolBinding,
    CapabilityToolOverride,
)
from assistant.core.capabilities.snapshot import (
    CapabilityLoadIssue,
    CapabilitySnapshot,
    ManifestFileState,
)

__all__ = [
    "CapabilityCatalog",
    "CapabilityCatalogInterface",
    "CapabilityDefinition",
    "CapabilityLoadError",
    "CapabilityLoadIssue",
    "CapabilitySnapshot",
    "CapabilityToolBinding",
    "CapabilityToolOverride",
    "ClaudeCodeSettingsSync",
    "ManifestFileState",
    "SubagentMcpServersSync",
    "apply_claude_code_settings",
    "collect_claude_code_mcp_servers",
    "collect_mcp_server_ids",
    "expand_nested_capabilities",
    "load_capability_definitions",
    "resolve_active_capabilities",
]
