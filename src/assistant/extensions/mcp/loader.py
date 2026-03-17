"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Load MCP tool mappings from plugins/mcp/*/tool_map.yaml.
"""

from pathlib import Path

import yaml

from assistant.core.config.loader import resolve_config_dir
from assistant.extensions.mcp.models import McpToolMapping


def _plugins_mcp_dir(config_dir: Path | None = None) -> Path:
    root = config_dir if config_dir is not None else resolve_config_dir()
    return root.parent / "plugins" / "mcp"


def discover_tool_mappings(plugins_mcp: Path) -> list[Path]:
    """Discover tool_map.yaml paths under plugins/mcp/."""
    if not plugins_mcp.is_dir():
        return []
    paths: list[Path] = []
    root_map = plugins_mcp / "tool_map.yaml"
    if root_map.exists():
        paths.append(root_map)
    for path in sorted(plugins_mcp.iterdir()):
        if path.is_dir():
            candidate = path / "tool_map.yaml"
            if candidate.exists():
                paths.append(candidate)
    return paths


def load_tool_mappings(config_dir: Path | str | None = None) -> dict[str, McpToolMapping]:
    """Load all MCP tool mappings keyed by server_id.

    Returns empty dict if plugins/mcp does not exist. Duplicate server_id is a startup error.
    """
    plugins_mcp = _plugins_mcp_dir(Path(config_dir) if isinstance(config_dir, str) else config_dir)
    paths = discover_tool_mappings(plugins_mcp)
    if not paths:
        return {}

    result: dict[str, McpToolMapping] = {}
    for path in paths:
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            if not isinstance(data, dict):
                continue
            mapping = McpToolMapping(**data)
            if mapping.server_id in result:
                raise ValueError(f"Duplicate MCP server_id in tool mappings: {mapping.server_id}")
            result[mapping.server_id] = mapping
        except Exception:
            raise
    return result


def capability_id_for_mcp_tool(server_id: str, tool_name: str) -> str:
    """Build capability ID per catalog convention: cap.mcp.<server>.<tool>."""
    return f"cap.mcp.{server_id}.{tool_name}"
