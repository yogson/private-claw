"""Tests for input_schema forwarding to Pydantic AI tools (P3)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent
from mcp.types import Tool as McpTool

from assistant.core.config.schemas import (
    McpDefaults,
    McpServerEntry,
    McpServersConfig,
    McpTimeouts,
    RuntimeConfig,
)
from assistant.extensions.mcp.bridge import (
    _PERMISSIVE_SCHEMA,
    McpBridge,
    _normalize_result,
    _resolve_input_schema,
    _unwrap_arguments,
)
from assistant.extensions.mcp.schema_cache import McpSchemaCache, McpToolSpec

_SERVER_SCHEMA = {
    "type": "object",
    "properties": {"holdings": {"type": "array", "items": {"type": "object"}}},
    "required": ["holdings"],
}
_MAPPED_SCHEMA = {
    "type": "object",
    "properties": {"symbols": {"type": "array"}},
    "required": ["symbols"],
}


def test_resolve_input_schema_prefers_server_schema() -> None:
    assert _resolve_input_schema(_SERVER_SCHEMA, _MAPPED_SCHEMA) is _SERVER_SCHEMA


def test_resolve_input_schema_falls_back_to_mapped_schema() -> None:
    assert _resolve_input_schema(None, _MAPPED_SCHEMA) is _MAPPED_SCHEMA


def test_resolve_input_schema_permissive_when_nothing_usable() -> None:
    assert _resolve_input_schema(None, None) is _PERMISSIVE_SCHEMA
    assert _resolve_input_schema({"type": "object"}, {}) is _PERMISSIVE_SCHEMA


def test_unwrap_arguments_undoes_wrapper() -> None:
    wrapped = {"arguments": {"ticker": "MSFT", "limit": 5}}
    assert _unwrap_arguments(wrapped, _MAPPED_SCHEMA) == {"ticker": "MSFT", "limit": 5}


def test_unwrap_arguments_keeps_real_arguments_param() -> None:
    schema = {"type": "object", "properties": {"arguments": {"type": "object"}}}
    kwargs = {"arguments": {"a": 1}}
    assert _unwrap_arguments(kwargs, schema) == kwargs


def test_unwrap_arguments_leaves_flat_args_alone() -> None:
    kwargs = {"ticker": "MSFT"}
    assert _unwrap_arguments(kwargs, _MAPPED_SCHEMA) == kwargs


def test_normalize_result_error_uses_plain_text() -> None:
    raw = CallToolResult(content=[TextContent(type="text", text="bad args")], isError=True)
    result = _normalize_result(raw)
    assert result["is_error"] is True
    assert result["error"] == "bad args"


def test_normalize_result_detects_soft_error_payload() -> None:
    raw = CallToolResult(content=[TextContent(type="text", text='{"error": "boom"}')])
    result = _normalize_result(raw)
    assert result["status"] == "error"
    assert result["error"] == "boom"


def test_normalize_result_keeps_payload_with_error_field_and_data() -> None:
    text = '{"error": null, "price": 1.0}'
    raw = CallToolResult(content=[TextContent(type="text", text=text)])
    result = _normalize_result(raw)
    assert result["status"] == "ok"
    assert result["content"] == text


class _FakeSession:
    async def list_tools(self) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                McpTool(
                    name="do_thing",
                    description="Holdings: list of {symbol, weight}.",
                    inputSchema=_SERVER_SCHEMA,
                )
            ],
        )


class _FakePool:
    @asynccontextmanager
    async def acquire(self, **_: Any) -> AsyncIterator[_FakeSession]:
        yield _FakeSession()


async def test_schema_cache_refresh_server_stores_schemas() -> None:
    cache = McpSchemaCache()
    await cache.refresh_server(
        "srv",
        {"transport": "sse", "url": "http://x"},
        _FakePool(),  # type: ignore[arg-type]
        connect_timeout=1.0,
        call_timeout=1.0,
    )
    assert cache.has_server("srv")
    assert cache.get("srv", "do_thing") == McpToolSpec(
        input_schema=_SERVER_SCHEMA, description="Holdings: list of {symbol, weight}."
    )


async def test_schema_cache_refresh_failure_is_swallowed() -> None:
    class _BrokenPool:
        @asynccontextmanager
        async def acquire(self, **_: Any) -> AsyncIterator[_FakeSession]:
            raise ConnectionError("down")
            yield

    cache = McpSchemaCache()
    await cache.refresh_server(
        "srv",
        {"transport": "sse", "url": "http://x"},
        _BrokenPool(),  # type: ignore[arg-type]
        connect_timeout=1.0,
        call_timeout=1.0,
    )
    assert not cache.has_server("srv")


def _make_config(tmp_path: Path) -> RuntimeConfig:
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        MemoryConfig,
        ModelConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolsConfig,
    )

    plugins_mcp = tmp_path / "plugins" / "mcp" / "test_server"
    plugins_mcp.mkdir(parents=True)
    (plugins_mcp / "tool_map.yaml").write_text(
        """
server_id: test_server
tools:
  - tool_name: do_thing
    summary: Do a thing
    risk_class: readonly
    input_schema:
      type: object
      properties:
        name:
          type: string
          description: The name
        count:
          type: integer
          description: How many
      required:
        - name
"""
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    return RuntimeConfig(
        app=AppConfig(data_root=str(tmp_path), timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(
            default_model_id="claude-3-5-haiku", model_allowlist=["claude-3-5-haiku"]
        ),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=["assistant"]),
        tools=ToolsConfig(tools=[]),
        mcp_servers=McpServersConfig(
            servers=[
                McpServerEntry(
                    id="test_server",
                    url="http://localhost:9999/sse",
                    enabled=True,
                    tool_policy="deny_by_default",
                )
            ],
            defaults=McpDefaults(),
            timeouts=McpTimeouts(),
        ),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
        config_dir=config_dir,
    )


def test_bridge_builds_tool_with_input_schema(tmp_path: Path) -> None:
    """Without a cached server schema, the tool_map.yaml schema is forwarded verbatim."""
    bridge = McpBridge(_make_config(tmp_path), schema_cache=McpSchemaCache())
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.test_server.do_thing"})
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "cap_mcp_test_server_do_thing"
    assert "Do a thing" in (tool.description or "")
    schema = tool.function_schema.json_schema
    assert set(schema["properties"]) == {"name", "count"}
    assert schema["required"] == ["name"]


def test_bridge_prefers_cached_server_spec(tmp_path: Path) -> None:
    cache = McpSchemaCache()
    cache.set_server(
        "test_server",
        {"do_thing": McpToolSpec(input_schema=_SERVER_SCHEMA, description="Server doc.")},
    )
    bridge = McpBridge(_make_config(tmp_path), schema_cache=cache)
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.test_server.do_thing"})
    assert tools[0].function_schema.json_schema == _SERVER_SCHEMA
    # Curated summary wins by default: server docstrings inflate the tool list.
    assert tools[0].description == "Do a thing"


def test_bridge_uses_server_description_when_tool_opts_in(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    tool_map = tmp_path / "plugins" / "mcp" / "test_server" / "tool_map.yaml"
    tool_map.write_text(
        tool_map.read_text().replace(
            "    summary: Do a thing\n",
            "    summary: Do a thing\n    use_server_description: true\n",
        )
    )
    cache = McpSchemaCache()
    cache.set_server(
        "test_server",
        {"do_thing": McpToolSpec(input_schema=_SERVER_SCHEMA, description="Server doc.")},
    )
    bridge = McpBridge(config, schema_cache=cache)
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.test_server.do_thing"})
    assert tools[0].description == "Server doc."


def test_bridge_falls_back_to_summary_without_server_description(tmp_path: Path) -> None:
    cache = McpSchemaCache()
    cache.set_server(
        "test_server",
        {"do_thing": McpToolSpec(input_schema=_SERVER_SCHEMA, description=None)},
    )
    bridge = McpBridge(_make_config(tmp_path), schema_cache=cache)
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.test_server.do_thing"})
    assert tools[0].description == "Do a thing"


class _RecordingSession:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any], **_: Any) -> CallToolResult:
        self.calls.append((name, arguments))
        return CallToolResult(content=[TextContent(type="text", text=self.reply)])


class _RecordingPool:
    def __init__(self, session: _RecordingSession) -> None:
        self.session = session

    @asynccontextmanager
    async def acquire(self, **_: Any) -> AsyncIterator[_RecordingSession]:
        yield self.session


async def test_agent_call_forwards_args_and_unwraps_wrapper(tmp_path: Path) -> None:
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from assistant.agent.deps import TurnDeps

    session = _RecordingSession(reply='{"ok": true}')
    bridge = McpBridge(
        _make_config(tmp_path),
        pool=_RecordingPool(session),  # type: ignore[arg-type]
        schema_cache=McpSchemaCache(),
    )
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.test_server.do_thing"})

    def model_fn(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="cap_mcp_test_server_do_thing",
                        args={"arguments": {"name": "x", "extra": 1}},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model_fn), deps_type=TurnDeps, tools=tools)
    deps = TurnDeps(writes_approved=[], seen_intent_ids=set())
    result = await agent.run("go", deps=deps)

    assert result.output == "done"
    # Unknown args are passed through for the server to judge, not rejected locally.
    assert session.calls == [("do_thing", {"name": "x", "extra": 1})]


async def test_schema_cache_refresh_bumps_generation() -> None:
    cache = McpSchemaCache()
    before = cache.generation
    await cache.refresh_server(
        "srv",
        {"transport": "sse", "url": "http://x"},
        _FakePool(),  # type: ignore[arg-type]
        connect_timeout=1.0,
        call_timeout=1.0,
    )
    assert cache.generation > before


async def test_schema_cache_ensure_refreshed_joins_scheduled_refresh() -> None:
    """Startup warm-up must not fetch list_tools again for a server whose lazy
    refresh (scheduled while building the default adapter) is still in flight."""
    acquired = 0

    class _CountingPool:
        @asynccontextmanager
        async def acquire(self, **_: Any) -> AsyncIterator[_FakeSession]:
            nonlocal acquired
            acquired += 1
            yield _FakeSession()

    cache = McpSchemaCache()
    kwargs: dict[str, Any] = {"connect_timeout": 1.0, "call_timeout": 1.0}
    connection = {"transport": "sse", "url": "http://x"}
    cache.schedule_refresh("srv", connection, _CountingPool(), **kwargs)  # type: ignore[arg-type]
    await cache.ensure_refreshed("srv", connection, _CountingPool(), **kwargs)  # type: ignore[arg-type]
    assert acquired == 1
    assert cache.has_server("srv")


class _CountingBrokenPool:
    def __init__(self) -> None:
        self.attempts = 0

    @asynccontextmanager
    async def acquire(self, **_: Any) -> AsyncIterator[_FakeSession]:
        self.attempts += 1
        raise ConnectionError("down")
        yield


async def test_schema_cache_backs_off_after_failed_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dead server must not be reconnected (or respawned) on every tool build."""
    now = [1000.0]
    monkeypatch.setattr("assistant.extensions.mcp.schema_cache.time.monotonic", lambda: now[0])
    pool = _CountingBrokenPool()
    cache = McpSchemaCache()
    kwargs: dict[str, Any] = {"connect_timeout": 1.0, "call_timeout": 1.0}
    connection = {"transport": "sse", "url": "http://x"}

    await cache.ensure_refreshed("srv", connection, pool, **kwargs)  # type: ignore[arg-type]
    assert pool.attempts == 1

    cache.schedule_refresh("srv", connection, pool, **kwargs)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert pool.attempts == 1  # still backing off

    now[0] += 31.0
    cache.schedule_refresh("srv", connection, pool, **kwargs)  # type: ignore[arg-type]
    await asyncio.sleep(0)
    assert pool.attempts == 2


async def test_schema_cache_cancel_pending_stops_in_flight_refresh() -> None:
    started = asyncio.Event()

    class _HangingPool:
        @asynccontextmanager
        async def acquire(self, **_: Any) -> AsyncIterator[_FakeSession]:
            started.set()
            await asyncio.Event().wait()
            yield _FakeSession()

    cache = McpSchemaCache()
    cache.schedule_refresh(
        "srv",
        {"transport": "sse", "url": "http://x"},
        _HangingPool(),  # type: ignore[arg-type]
        connect_timeout=1.0,
        call_timeout=1.0,
    )
    await started.wait()
    await cache.cancel_pending()
    assert not cache.has_server("srv")


async def test_refresh_schemas_only_touches_servers_in_capability_ids(tmp_path: Path) -> None:
    refreshed: list[str] = []

    class _RecordingCache(McpSchemaCache):
        async def ensure_refreshed(self, server_id: str, *args: Any, **kwargs: Any) -> None:
            refreshed.append(server_id)

    bridge = McpBridge(_make_config(tmp_path), schema_cache=_RecordingCache())
    await bridge.refresh_schemas({"assistant", "cap.mcp.other_server.x"})
    assert refreshed == []
    await bridge.refresh_schemas({"cap.mcp.test_server.do_thing"})
    assert refreshed == ["test_server"]


async def test_refresh_schemas_skips_denied_server(tmp_path: Path) -> None:
    refreshed: list[str] = []

    class _RecordingCache(McpSchemaCache):
        async def ensure_refreshed(self, server_id: str, *args: Any, **kwargs: Any) -> None:
            refreshed.append(server_id)

    config = _make_config(tmp_path)
    config.mcp_servers.servers[0].tool_policy = "deny"
    bridge = McpBridge(config, schema_cache=_RecordingCache())
    await bridge.refresh_schemas({"cap.mcp.test_server.do_thing"})
    assert refreshed == []
