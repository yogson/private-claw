"""
Component ID: CMP_CORE_CAPABILITIES

Per-run sub-agent MCP servers for an explicit set of capabilities.

The default delegation path hands every sub-agent the Claude Code MCP servers of the
globally active capabilities (see ``SubagentMcpServersSync``). Some runs - agent-node
ingress jobs - instead name the capabilities they run under. For those, this module
builds exactly the servers those capabilities grant:

* ``claude_code_settings.mcp_servers`` - Claude Code server specs, used as-is;
* ``mcp_servers`` - ids of main-agent servers from ``mcp_servers.yaml``, translated to
  Claude Code specs. Their tool restrictions carry over as a :class:`McpToolFilter`:
  ``include_tools`` limits the server to those tools, while ``exclude_tools`` and
  ``confirm_tools`` are blocked (an unattended sub-agent cannot ask for confirmation).

Specs can hold secrets (headers, env), so they are resolved at execution time from the
capability ids and never stored in task records.
"""

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from assistant.core.capabilities.resolver import expand_nested_capabilities
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.config.schemas import McpServerEntry, McpServersConfig

logger = structlog.get_logger(__name__)

MCP_TOOL_PREFIX = "mcp__"


@dataclass(frozen=True)
class McpToolFilter:
    """Tool restrictions for one server, in server-side tool names."""

    include: frozenset[str] | None = None
    deny: frozenset[str] = frozenset()

    def allows(self, tool: str) -> bool:
        if tool in self.deny:
            return False
        return self.include is None or tool in self.include


@dataclass(frozen=True)
class SubagentMcp:
    """MCP servers for one sub-agent run, plus per-server tool filters."""

    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_filters: dict[str, McpToolFilter] = field(default_factory=dict)

    def denial_reason(self, tool_name: str) -> str | None:
        """Why ``tool_name`` (``mcp__<server>__<tool>``) is blocked, or None if allowed."""
        if not tool_name.startswith(MCP_TOOL_PREFIX):
            return None
        rest = tool_name[len(MCP_TOOL_PREFIX) :]
        # Longest server id first, so a server whose id prefixes another's cannot
        # capture the other's tools.
        for server_id in sorted(self.tool_filters, key=len, reverse=True):
            prefix = f"{server_id}__"
            if rest.startswith(prefix):
                tool = rest[len(prefix) :]
                if self.tool_filters[server_id].allows(tool):
                    return None
                return f"Tool {tool!r} of MCP server {server_id!r} is not available to this run."
        return None


SubagentMcpResolver = Callable[[Sequence[str]], SubagentMcp]


def claude_code_spec(entry: McpServerEntry) -> dict[str, Any]:
    """Translate a ``mcp_servers.yaml`` entry into a Claude Code ``mcpServers`` spec."""
    if entry.transport == "stdio":
        spec: dict[str, Any] = {"type": "stdio", "command": entry.command, "args": list(entry.args)}
        if entry.env:
            spec["env"] = dict(entry.env)
        return spec
    spec = {"type": "http" if entry.transport == "streamable-http" else "sse", "url": entry.url}
    if entry.headers:
        spec["headers"] = dict(entry.headers)
    return spec


def _tool_filter(entry: McpServerEntry) -> McpToolFilter | None:
    deny = frozenset([*(entry.exclude_tools or []), *entry.confirm_tools])
    include = frozenset(entry.include_tools) if entry.include_tools is not None else None
    if include is None and not deny:
        return None
    return McpToolFilter(include=include, deny=deny)


def build_subagent_mcp(
    capability_ids: Sequence[str],
    definitions: Mapping[str, CapabilityDefinition],
    mcp_config: McpServersConfig,
) -> SubagentMcp:
    """Servers granted by ``capability_ids`` (nested capabilities expanded).

    Later capabilities win on a server id clash, as in the default path. Unknown or
    disabled main-agent server ids are skipped with a warning.
    """
    entries = {entry.id: entry for entry in mcp_config.servers}
    servers: dict[str, dict[str, Any]] = {}
    filters: dict[str, McpToolFilter] = {}
    for cap_id in expand_nested_capabilities(capability_ids, definitions):
        definition = definitions.get(cap_id)
        if definition is None:
            continue
        if definition.claude_code_settings is not None:
            for name, spec in definition.claude_code_settings.mcp_servers.items():
                servers[name] = copy.deepcopy(spec)
                filters.pop(name, None)
        for server_id in definition.mcp_servers:
            entry = entries.get(server_id)
            if entry is None or not entry.enabled:
                logger.warning(
                    "subagent_mcp.server_unavailable",
                    capability_id=cap_id,
                    server_id=server_id,
                    reason="unknown" if entry is None else "disabled",
                )
                continue
            servers[server_id] = claude_code_spec(entry)
            tool_filter = _tool_filter(entry)
            if tool_filter is None:
                filters.pop(server_id, None)
            else:
                filters[server_id] = tool_filter
    return SubagentMcp(servers=servers, tool_filters=filters)


def resolve_run_mcp(
    backend_params: Mapping[str, Any],
    default_servers: Mapping[str, dict[str, Any]],
    resolver: SubagentMcpResolver | None,
) -> SubagentMcp:
    """MCP for one run: ``backend_params["mcp_capabilities"]`` if given, else the default.

    A run that names its capabilities gets exactly their servers - an empty list means
    no MCP servers at all. Without a resolver such a run gets none, with a warning.
    """
    caps = backend_params.get("mcp_capabilities")
    if not isinstance(caps, list):
        return SubagentMcp(servers=dict(default_servers))
    if resolver is None:
        logger.warning("subagent_mcp.no_resolver", capability_ids=caps)
        return SubagentMcp()
    return resolver([str(c) for c in caps])
