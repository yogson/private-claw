"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP bridge: maps enabled MCP tools to Pydantic AI tools with risk-class policy.
"""

from typing import Any

import structlog
from pydantic_ai import RunContext, Tool

from assistant.agent.deps import TurnDeps
from assistant.core.config.loader import resolve_config_dir
from assistant.core.config.schemas import RuntimeConfig
from assistant.extensions.mcp.client import call_mcp_tool
from assistant.extensions.mcp.loader import (
    load_tool_mappings,
)
from assistant.extensions.mcp.models import RiskClass

logger = structlog.get_logger(__name__)


def _config_dir(config: RuntimeConfig) -> str | None:
    if config.config_dir is not None:
        return str(config.config_dir)
    return str(resolve_config_dir())


def _get_server_url(config: RuntimeConfig, server_id: str) -> str | None:
    for srv in config.mcp_servers.servers:
        if srv.id == server_id and srv.enabled:
            return srv.url
    return None


def _effective_tool_policy(config: RuntimeConfig, server_id: str) -> str:
    """Resolve effective tool_policy: per-server override or defaults."""
    for srv in config.mcp_servers.servers:
        if srv.id == server_id:
            return srv.tool_policy
    return config.mcp_servers.defaults.tool_policy


def _build_mcp_tool(
    capability_id: str,
    server_id: str,
    tool_name: str,
    summary: str,
    risk_class: RiskClass,
    requires_confirmation: bool,
    url: str,
    connect_timeout: float,
    call_timeout: float,
) -> Tool[TurnDeps]:
    """Build a Pydantic AI Tool that invokes MCP."""

    async def _invoke(
        ctx: RunContext[TurnDeps],
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await call_mcp_tool(
            url,
            tool_name,
            arguments or {},
            connect_timeout=connect_timeout,
            call_timeout=call_timeout,
        )
        logger.info(
            "mcp.tool_invoked",
            capability_id=capability_id,
            mcp_server=server_id,
            mcp_tool=tool_name,
            risk_class=risk_class.value,
            status=result.get("status", "unknown"),
        )
        return result

    desc = summary or f"MCP tool {tool_name} from server {server_id}."
    if requires_confirmation or risk_class != RiskClass.READONLY:
        desc += f" [Risk: {risk_class.value}; requires_confirmation={requires_confirmation}]"
    return Tool(
        _invoke,
        name=capability_id,
        description=desc,
    )


class McpBridge:
    """MCP bridge: loads mappings, exposes allowlisted tools as Pydantic AI tools."""

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config
        self._mappings = load_tool_mappings(_config_dir(config))

    def get_tools_for_capability_ids(self, capability_ids: set[str]) -> list[Tool[TurnDeps]]:
        """Return Pydantic AI tools for enabled MCP capability IDs."""
        tools: list[Tool[TurnDeps]] = []
        connect_timeout = float(self._config.mcp_servers.timeouts.connect_seconds)
        call_timeout = float(self._config.mcp_servers.timeouts.call_seconds)

        for cap_id in sorted(capability_ids):
            if not cap_id.startswith("cap.mcp."):
                continue
            parts = cap_id.split(".")
            if len(parts) != 4:
                continue
            _, _, server_id, tool_name = parts
            mapping = self._mappings.get(server_id)
            if mapping is None:
                logger.debug(
                    "mcp.tool_skipped",
                    capability_id=cap_id,
                    reason="no tool mapping for server",
                )
                continue
            mapped = next((t for t in mapping.tools if t.tool_name == tool_name), None)
            if mapped is None:
                logger.debug(
                    "mcp.tool_skipped",
                    capability_id=cap_id,
                    reason="tool not in allowlist",
                )
                continue
            url = _get_server_url(self._config, server_id)
            if not url:
                logger.debug(
                    "mcp.tool_skipped",
                    capability_id=cap_id,
                    reason="server not enabled or missing url",
                )
                continue
            policy = _effective_tool_policy(self._config, server_id)
            if policy == "deny":
                logger.debug(
                    "mcp.tool_skipped",
                    capability_id=cap_id,
                    reason="server tool_policy=deny blocks all tools",
                )
                continue
            tool = _build_mcp_tool(
                capability_id=cap_id,
                server_id=server_id,
                tool_name=tool_name,
                summary=mapped.summary,
                risk_class=mapped.risk_class,
                requires_confirmation=mapped.requires_confirmation,
                url=url,
                connect_timeout=connect_timeout,
                call_timeout=call_timeout,
            )
            tools.append(tool)
        return tools
