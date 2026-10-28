"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP bridge: maps enabled MCP tools to Pydantic AI tools with risk-class policy.
"""

import json
from datetime import timedelta
from typing import Any

import structlog
from mcp.types import CallToolResult
from pydantic_ai import RunContext, Tool

from assistant.agent.deps import TurnDeps
from assistant.core.config.loader import resolve_config_dir
from assistant.core.config.schemas import RuntimeConfig
from assistant.extensions.mcp.confirmation import check_confirmation
from assistant.extensions.mcp.loader import (
    load_tool_mappings,
)
from assistant.extensions.mcp.models import McpMappedTool, RiskClass
from assistant.extensions.mcp.schema_cache import McpSchemaCache, McpToolSpec
from assistant.extensions.mcp.session_pool import McpSessionPool

logger = structlog.get_logger(__name__)

# Shared pool: one subprocess/connection per MCP server, reused across all turns.
# Idle sessions are kept alive for up to 10 minutes, then killed by the sweeper.
_MCP_IDLE_TTL = 600.0  # 10 minutes
mcp_pool = McpSessionPool(idle_ttl=_MCP_IDLE_TTL)
# Tool input schemas as advertised by the servers (preferred over tool_map.yaml).
mcp_schema_cache = McpSchemaCache()

# Used when neither the server nor tool_map.yaml provides a schema.
_PERMISSIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}
# Truncation limit for error text in logs.
_LOG_ERROR_MAX_CHARS = 500


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
                "call_seconds": srv.call_seconds,
            }
    return None


def _effective_tool_policy(config: RuntimeConfig, server_id: str) -> str:
    """Resolve effective tool_policy: per-server override or defaults."""
    for srv in config.mcp_servers.servers:
        if srv.id == server_id:
            return srv.tool_policy
    return config.mcp_servers.defaults.tool_policy


def _resolve_input_schema(
    fetched_schema: dict[str, Any] | None, mapped_schema: dict[str, Any] | None
) -> dict[str, Any]:
    """Pick the tool's JSON schema: server-advertised > tool_map.yaml > permissive."""
    for schema in (fetched_schema, mapped_schema):
        if schema and isinstance(schema.get("properties"), dict):
            return schema
    return _PERMISSIVE_SCHEMA


def _resolve_description(spec: McpToolSpec | None, mapped: McpMappedTool) -> str:
    """Pick the tool description: curated summary unless the tool opts into the server's."""
    server_description = spec.description if spec else None
    if mapped.use_server_description and server_description:
        return server_description
    return mapped.summary or server_description or ""


def _unwrap_arguments(kwargs: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Undo a common model mistake: wrapping all args in {"arguments": {...}}."""
    inner = kwargs.get("arguments")
    if (
        len(kwargs) == 1
        and isinstance(inner, dict)
        and "arguments" not in schema.get("properties", {})
    ):
        return inner
    return kwargs


def _text_parts(raw: CallToolResult) -> list[str]:
    return [block.text for block in (raw.content or []) if hasattr(block, "text") and block.text]


def _soft_error(text: str) -> str | None:
    """Detect servers that report failures as a successful {"error": "..."} payload."""
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if isinstance(payload, dict) and set(payload) == {"error"} and payload["error"]:
        return str(payload["error"])
    return None


def _normalize_result(raw: CallToolResult) -> dict[str, Any]:
    """Normalize an MCP CallToolResult into a flat dict."""
    parts = _text_parts(raw)
    text = "\n".join(parts) if parts else None
    if raw.isError:
        return {
            "status": "error",
            "content": None,
            "error": text or "Unknown MCP error",
            "is_error": True,
        }
    soft_error = _soft_error(text) if text else None
    if soft_error is not None:
        return {"status": "error", "content": None, "error": soft_error, "is_error": True}
    return {
        "status": "ok",
        "content": text,
        "error": None,
        "is_error": False,
    }


def _build_mcp_tool(
    capability_id: str,
    server_id: str,
    tool_name: str,
    summary: str,
    risk_class: RiskClass,
    requires_confirmation: bool,
    input_schema: dict[str, Any],
    connection: dict[str, Any],
    connect_timeout: float,
    call_timeout: float,
    pool: McpSessionPool,
) -> Tool[TurnDeps]:
    """Build a Pydantic AI Tool that invokes MCP via the shared session pool.

    The JSON schema is forwarded verbatim; argument validation is left to the
    MCP server, whose error text is returned to the model so it can self-correct.
    """

    url = connection["url"] or None
    transport = connection.get("transport", "sse")
    command = connection.get("command") or None
    args = connection.get("args") or None
    env = connection.get("env") or None

    async def _call(arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            async with pool.acquire(
                transport=transport,
                url=url,
                command=command,
                args=args,
                env=env,
                connect_timeout=connect_timeout,
                call_timeout=call_timeout,
            ) as session:
                raw = await session.call_tool(
                    tool_name,
                    arguments,
                    read_timeout_seconds=timedelta(seconds=call_timeout),
                )
                return _normalize_result(raw)
        except Exception as exc:
            logger.warning(
                "mcp.call_failed",
                tool_name=tool_name,
                transport=transport,
                error=str(exc),
            )
            return {"status": "error", "content": None, "error": str(exc), "is_error": True}

    async def _invoke(ctx: RunContext[TurnDeps], **kwargs: Any) -> dict[str, Any]:
        arguments = _unwrap_arguments(kwargs, input_schema)
        if requires_confirmation:
            await check_confirmation(ctx, capability_id, server_id, tool_name, arguments)
        result = await _call(arguments)
        log_fields: dict[str, Any] = {}
        if result.get("is_error"):
            log_fields["error"] = str(result.get("error"))[:_LOG_ERROR_MAX_CHARS]
            log_fields["arg_names"] = sorted(arguments)
        logger.info(
            "mcp.tool_invoked",
            capability_id=capability_id,
            mcp_server=server_id,
            mcp_tool=tool_name,
            risk_class=risk_class.value,
            status=result.get("status", "unknown"),
            **log_fields,
        )
        return result

    desc = summary or f"MCP tool {tool_name} from server {server_id}."
    if requires_confirmation or risk_class != RiskClass.READONLY:
        desc += f" [Risk: {risk_class.value}; requires_confirmation={requires_confirmation}]"
    return Tool.from_schema(
        _invoke,
        name=capability_id.replace(".", "_"),
        description=desc,
        json_schema=input_schema,
        takes_ctx=True,
    )


class McpBridge:
    """MCP bridge: loads mappings, exposes allowlisted tools as Pydantic AI tools."""

    def __init__(
        self,
        config: RuntimeConfig,
        pool: McpSessionPool | None = None,
        schema_cache: McpSchemaCache | None = None,
    ) -> None:
        self._config = config
        self._mappings = load_tool_mappings(_config_dir(config))
        self._pool = pool if pool is not None else mcp_pool
        self._schema_cache = schema_cache if schema_cache is not None else mcp_schema_cache

    async def close(self) -> None:
        """Stop in-flight schema refreshes so none outlives the session pool."""
        await self._schema_cache.cancel_pending()

    async def refresh_schemas(self, capability_ids: set[str]) -> None:
        """Fetch input schemas from the servers that ``capability_ids`` actually use.

        Servers no enabled cap.mcp.* id references, or whose tool_policy is deny,
        are skipped so warm-up never spawns a subprocess nothing will call.
        """
        connect_timeout = float(self._config.mcp_servers.timeouts.connect_seconds)
        server_ids = {
            parts[2]
            for cap_id in capability_ids
            if len(parts := cap_id.split(".")) == 4 and cap_id.startswith("cap.mcp.")
        }
        for server_id in sorted(server_ids & self._mappings.keys()):
            connection = _get_server_connection(self._config, server_id)
            if connection is None or _effective_tool_policy(self._config, server_id) == "deny":
                continue
            await self._schema_cache.ensure_refreshed(
                server_id,
                connection,
                self._pool,
                connect_timeout=connect_timeout,
                call_timeout=self._call_timeout(connection),
            )

    def _call_timeout(self, connection: dict[str, Any]) -> float:
        per_server = connection.get("call_seconds")
        if per_server is not None:
            return float(per_server)
        return float(self._config.mcp_servers.timeouts.call_seconds)

    def get_tools_for_capability_ids(self, capability_ids: set[str]) -> list[Tool[TurnDeps]]:
        """Return Pydantic AI tools for enabled MCP capability IDs."""
        tools: list[Tool[TurnDeps]] = []
        connect_timeout = float(self._config.mcp_servers.timeouts.connect_seconds)

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
            call_timeout = self._call_timeout(connection)
            if not self._schema_cache.has_server(server_id):
                self._schema_cache.schedule_refresh(
                    server_id,
                    connection,
                    self._pool,
                    connect_timeout=connect_timeout,
                    call_timeout=call_timeout,
                )
            spec = self._schema_cache.get(server_id, tool_name)
            tool = _build_mcp_tool(
                capability_id=cap_id,
                server_id=server_id,
                tool_name=tool_name,
                summary=_resolve_description(spec, mapped),
                risk_class=mapped.risk_class,
                requires_confirmation=mapped.requires_confirmation,
                input_schema=_resolve_input_schema(
                    spec.input_schema if spec else None, mapped.input_schema
                ),
                connection=connection,
                connect_timeout=connect_timeout,
                call_timeout=call_timeout,
                pool=self._pool,
            )
            tools.append(tool)
        return tools
