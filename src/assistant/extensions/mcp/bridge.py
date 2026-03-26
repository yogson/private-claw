"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP bridge: maps enabled MCP tools to Pydantic AI tools with risk-class policy.
"""

from typing import Any

import structlog
from pydantic import create_model
from pydantic.fields import FieldInfo
from pydantic_ai import RunContext, Tool

from assistant.agent.deps import TurnDeps
from assistant.core.config.loader import resolve_config_dir
from assistant.core.config.schemas import RuntimeConfig
from assistant.extensions.mcp.client import call_mcp_tool
from assistant.extensions.mcp.confirmation import check_confirmation
from assistant.extensions.mcp.loader import (
    load_tool_mappings,
)
from assistant.extensions.mcp.models import RiskClass

logger = structlog.get_logger(__name__)

# JSON Schema type → Python type mapping for input_schema forwarding
_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _config_dir(config: RuntimeConfig) -> str | None:
    if config.config_dir is not None:
        return str(config.config_dir)
    return str(resolve_config_dir())


def _get_server_connection(config: RuntimeConfig, server_id: str) -> dict[str, Any] | None:
    """Return connection details for an enabled server, or None."""
    for srv in config.mcp_servers.servers:
        if srv.id == server_id and srv.enabled:
            return {
                "url": srv.url,
                "transport": srv.transport,
                "command": srv.command,
                "args": srv.args,
                "env": srv.env or None,
            }
    return None


def _effective_tool_policy(config: RuntimeConfig, server_id: str) -> str:
    """Resolve effective tool_policy: per-server override or defaults."""
    for srv in config.mcp_servers.servers:
        if srv.id == server_id:
            return srv.tool_policy
    return config.mcp_servers.defaults.tool_policy


def _build_input_model(capability_id: str, input_schema: dict[str, Any] | None) -> type | None:
    """Build a Pydantic model from JSON Schema so the LLM gets parameter descriptions.

    Returns None if no usable schema is provided (falls back to generic dict).
    """
    if not input_schema:
        return None
    properties = input_schema.get("properties")
    if not properties or not isinstance(properties, dict):
        return None

    required = set(input_schema.get("required", []))
    fields: dict[str, Any] = {}
    for name, prop in properties.items():
        json_type = prop.get("type", "string")
        py_type = _JSON_TYPE_MAP.get(json_type, Any)
        description = prop.get("description", "")
        default = ... if name in required else prop.get("default")
        fields[name] = (py_type, FieldInfo(default=default, description=description))

    if not fields:
        return None

    # Sanitise capability_id into a valid Python class name
    model_name = capability_id.replace(".", "_").replace("-", "_") + "_Input"
    return create_model(model_name, **fields)  # type: ignore[no-any-return]


def _build_mcp_tool(
    capability_id: str,
    server_id: str,
    tool_name: str,
    summary: str,
    risk_class: RiskClass,
    requires_confirmation: bool,
    input_schema: dict[str, Any] | None,
    connection: dict[str, Any],
    connect_timeout: float,
    call_timeout: float,
) -> Tool[TurnDeps]:
    """Build a Pydantic AI Tool that invokes MCP."""

    url = connection["url"]
    transport = connection.get("transport", "sse")
    command = connection.get("command")
    args = connection.get("args")
    env = connection.get("env")

    input_model = _build_input_model(capability_id, input_schema)

    if input_model is not None:

        async def _invoke_typed(
            ctx: RunContext[TurnDeps],
            **kwargs: Any,
        ) -> dict[str, Any]:
            if requires_confirmation:
                await check_confirmation(ctx, capability_id, server_id, tool_name, kwargs)
            result = await call_mcp_tool(
                url,
                tool_name,
                kwargs or {},
                transport=transport,
                command=command,
                args=args,
                env=env,
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

        # Apply the generated model's annotations so PydanticAI sees typed params
        _invoke_typed.__annotations__ = {
            "ctx": RunContext[TurnDeps],
            "return": dict[str, Any],
            **{k: v for k, v in input_model.__annotations__.items()},
        }

        desc = summary or f"MCP tool {tool_name} from server {server_id}."
        if requires_confirmation or risk_class != RiskClass.READONLY:
            desc += f" [Risk: {risk_class.value}; requires_confirmation={requires_confirmation}]"
        safe_name = capability_id.replace(".", "_")
        return Tool(
            _invoke_typed,
            name=safe_name,
            description=desc,
        )

    # Fallback: generic dict arguments (no input_schema provided)
    async def _invoke(
        ctx: RunContext[TurnDeps],
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if requires_confirmation:
            await check_confirmation(ctx, capability_id, server_id, tool_name, arguments)
        result = await call_mcp_tool(
            url,
            tool_name,
            arguments or {},
            transport=transport,
            command=command,
            args=args,
            env=env,
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
    safe_name = capability_id.replace(".", "_")
    return Tool(
        _invoke,
        name=safe_name,
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
            connection = _get_server_connection(self._config, server_id)
            if not connection:
                logger.debug(
                    "mcp.tool_skipped",
                    capability_id=cap_id,
                    reason="server not enabled or not configured",
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
                input_schema=mapped.input_schema,
                connection=connection,
                connect_timeout=connect_timeout,
                call_timeout=call_timeout,
            )
            tools.append(tool)
        return tools
