"""Tests for the native MCP toolsets: naming, filtering, lifecycle, reconnect, confirmation."""

import json
from typing import Any

import anyio
import httpx
import httpx2
import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import McpError
from mcp.types import ErrorData
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import ToolDefinition

from assistant.agent.deps import TurnDeps
from assistant.core.config.schemas import McpServerEntry, McpServersConfig
from assistant.extensions.mcp.confirmation import McpConfirmationDenied
from assistant.extensions.mcp.toolsets import (
    McpConnection,
    McpToolsets,
    build_mcp_toolset,
    build_server_toolset,
    is_connection_error,
    is_mcp_tool_name,
    tool_requires_confirmation,
)


def _server() -> FastMCP:
    mcp = FastMCP("demo", instructions="Use demo tools carefully.")

    @mcp.tool(annotations={"readOnlyHint": True})
    def echo(text: str) -> str:
        """Echo text back."""
        return f"echo:{text}"

    @mcp.tool(annotations={"destructiveHint": True, "readOnlyHint": False})
    def update_thing(name: str) -> str:
        """Update a thing."""
        return f"updated:{name}"

    @mcp.tool()
    def notify(name: str) -> str:
        """Plain tool, confirmed only via config."""
        return f"notified:{name}"

    @mcp.tool(annotations={"destructiveHint": True, "readOnlyHint": True})
    def odd(name: str) -> str:
        """Contradictory hints: readOnly wins."""
        return name

    @mcp.tool()
    def boom() -> str:
        """Always fails with an ordinary tool error."""
        raise ValueError("kaboom")

    return mcp


class _Factory:
    """Builds in-memory MCPToolsets and counts how many were created / entered."""

    def __init__(self) -> None:
        self.built: list[MCPToolset[Any]] = []

    def __call__(self, entry: McpServerEntry, connect: float, call: float) -> MCPToolset[Any]:
        ts: MCPToolset[Any] = MCPToolset(
            _server(),
            id=entry.id,
            tool_error_behavior="failed",
            max_retries=1,
            include_instructions=entry.include_instructions,
        )
        self.built.append(ts)
        return ts


def _entry(**kw: Any) -> McpServerEntry:
    return McpServerEntry(id="demo", transport="sse", url="http://localhost/sse", **kw)


def _connection(factory: _Factory | None = None, **kw: Any) -> tuple[McpConnection, _Factory]:
    factory = factory or _Factory()
    return McpConnection(_entry(**kw), factory=factory), factory


def _model_calling(tool: str, args: dict[str, Any]) -> FunctionModel:
    """First response calls ``tool``; once a tool result exists it answers with its text."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [p for m in messages for p in m.parts if p.part_kind == "tool-return"]
        retries = [p for m in messages for p in m.parts if p.part_kind == "retry-prompt"]
        if returns or retries:
            part = (returns or retries)[-1]
            return ModelResponse(parts=[TextPart(content=f"RESULT:{part.model_response_str()}")])
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args=args)])

    return FunctionModel(fn)


async def _offered(toolset: Any) -> list[ToolDefinition]:
    """Tool definitions the model is offered on a run using ``toolset``."""
    seen: list[AgentInfo] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart(content="ok")])

    await Agent(FunctionModel(fn), toolsets=[toolset]).run("hi")
    return list(seen[0].function_tools)


@pytest.fixture
async def started() -> Any:
    conn, factory = _connection()
    assert await conn.start()
    yield conn, factory
    await conn.stop()


# --- naming / filtering / schemas -------------------------------------------------------


async def test_tools_are_prefixed_with_real_schemas(started: Any) -> None:
    conn, _ = started
    toolset = build_server_toolset(conn)
    tools = {t.name: t for t in await _offered(toolset)}
    assert set(tools) == {
        "mcp_demo_echo",
        "mcp_demo_update_thing",
        "mcp_demo_notify",
        "mcp_demo_odd",
        "mcp_demo_boom",
    }
    assert all(is_mcp_tool_name(n) for n in tools)
    schema = tools["mcp_demo_echo"].parameters_json_schema
    assert schema["properties"]["text"]["type"] == "string"
    assert tools["mcp_demo_echo"].description == "Echo text back."


async def test_include_and_exclude_filters() -> None:
    for kwargs, expected in (
        ({"include_tools": ["echo", "notify"]}, {"mcp_demo_echo", "mcp_demo_notify"}),
        (
            {"exclude_tools": ["update_thing", "odd", "boom", "notify"]},
            {"mcp_demo_echo"},
        ),
    ):
        conn, _ = _connection(**kwargs)
        assert await conn.start()
        offered = await _offered(build_server_toolset(conn))
        assert {t.name for t in offered} == expected
        await conn.stop()


async def _instructions_seen(toolset: Any) -> str:
    seen: list[list[ModelMessage]] = []

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(messages)
        return ModelResponse(parts=[TextPart(content="ok")])

    await Agent(FunctionModel(fn), toolsets=[toolset]).run("hi")
    return " ".join(str(getattr(m, "instructions", "") or "") for m in seen[0])


async def test_server_instructions_toggle() -> None:
    texts: dict[bool, str] = {}
    for flag in (True, False):
        conn, _ = _connection(include_instructions=flag)
        assert await conn.start()
        texts[flag] = await _instructions_seen(build_server_toolset(conn))
        await conn.stop()
    assert "Use demo tools carefully." in texts[True]
    assert "Use demo tools carefully." not in texts[False]


# --- lifecycle --------------------------------------------------------------------------


async def test_connection_shared_across_runs_without_respawn(started: Any) -> None:
    conn, factory = started
    toolset = build_server_toolset(conn)
    for i in range(3):  # three "turns"
        agent: Agent[None, str] = Agent(
            _model_calling("mcp_demo_echo", {"text": str(i)}), toolsets=[toolset]
        )
        result = await agent.run("go")
        assert f"echo:{i}" in result.output
    assert len(factory.built) == 1, "the server must be connected exactly once"
    assert conn.connected


async def test_stop_closes_and_is_idempotent() -> None:
    conn, factory = _connection()
    await conn.start()
    await conn.stop()
    await conn.stop()
    assert not conn.connected
    assert not factory.built[0].is_running


async def test_failed_start_does_not_raise_and_retries_with_backoff() -> None:
    now = [100.0]
    attempts: list[int] = []

    class _Failing(_Factory):
        def __call__(self, entry: McpServerEntry, connect: float, call: float) -> MCPToolset[Any]:
            ts = super().__call__(entry, connect, call)
            attempts.append(1)
            if len(attempts) < 3:

                async def _fail() -> Any:
                    raise ConnectionRefusedError("down")

                ts.__aenter__ = _fail  # type: ignore[method-assign]
            return ts

    conn = McpConnection(_entry(), factory=_Failing(), clock=lambda: now[0])
    assert await conn.start() is False  # startup: warns, no raise
    assert not conn.connected
    assert await conn.start() is False and len(attempts) == 1  # inside the backoff window
    now[0] += 5.1
    assert await conn.start() is False and len(attempts) == 2  # retried, backoff doubled
    now[0] += 5.1
    assert await conn.start() is False and len(attempts) == 2  # 10s window not over yet
    now[0] += 5.0
    assert await conn.start() is True and len(attempts) == 3  # recovered lazily
    await conn.stop()


async def test_unavailable_server_contributes_no_tools_and_does_not_break_run() -> None:
    class _Down(_Factory):
        def __call__(self, entry: McpServerEntry, connect: float, call: float) -> MCPToolset[Any]:
            ts = super().__call__(entry, connect, call)

            async def _fail() -> Any:
                raise ConnectionRefusedError("down")

            ts.__aenter__ = _fail  # type: ignore[method-assign]
            return ts

    conn = McpConnection(_entry(), factory=_Down())
    assert await _offered(build_server_toolset(conn)) == []


async def test_registry_builds_shared_connections_and_skips_disabled() -> None:
    factory = _Factory()
    registry = McpToolsets(factory=factory)
    config = McpServersConfig(
        servers=[
            McpServerEntry(id="demo", url="http://h/sse"),
            McpServerEntry(id="off", url="http://h/sse", enabled=False),
        ]
    )
    first = registry.toolsets_for(config, ["demo", "off", "ghost"])
    second = registry.toolsets_for(config, ["demo"])
    assert len(first) == 1 and len(second) == 1
    assert await registry.start(config, ["demo", "off"]) == {"demo": True}
    await Agent(_model_calling("mcp_demo_echo", {"text": "a"}), toolsets=first).run("go")
    await Agent(_model_calling("mcp_demo_echo", {"text": "b"}), toolsets=second).run("go")
    assert len(factory.built) == 1
    await registry.close()
    assert not factory.built[0].is_running


# --- error behaviour / reconnect ---------------------------------------------------------


async def test_ordinary_tool_error_goes_to_model_without_reconnect(started: Any) -> None:
    conn, factory = started
    agent: Agent[None, str] = Agent(
        _model_calling("mcp_demo_boom", {}), toolsets=[build_server_toolset(conn)]
    )
    result = await agent.run("go")
    assert "kaboom" in result.output
    assert len(factory.built) == 1, "a tool error must not trigger a reconnect"


def _mcp_error(code: int, message: str) -> McpError:
    """Build the SDK's protocol error (positional ErrorData in mcp 1, code/message in mcp 2)."""
    try:
        return McpError(ErrorData(code=code, message=message))
    except TypeError:
        return McpError(code, message)  # type: ignore[call-arg,arg-type]


def _session_terminated() -> McpError:
    return _mcp_error(32600, "Session terminated")


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (anyio.ClosedResourceError(), True),
        (anyio.BrokenResourceError(), True),
        (anyio.EndOfStream(), True),
        (ConnectionResetError("reset"), True),
        (BrokenPipeError(), True),
        (httpx.ConnectError("refused"), True),
        (httpx.RemoteProtocolError("closed"), True),
        (httpx.ReadTimeout("slow"), False),
        (httpx.ConnectTimeout("slow"), False),
        (_session_terminated(), True),
        (_mcp_error(-32000, "Connection closed"), True),
        (
            RuntimeError("Client is not connected. Use the 'async with client:' context manager"),
            True,
        ),
        (_mcp_error(408, "Timed out while waiting for response"), False),
        (_mcp_error(-32602, "Invalid params"), False),
        (ValueError("kaboom"), False),
        (RuntimeError("something else"), False),
        (TimeoutError(), False),
        (ExceptionGroup("g", [ValueError("x"), anyio.ClosedResourceError()]), True),
        (ExceptionGroup("g", [ValueError("x"), KeyError("y")]), False),
    ],
)
def test_is_connection_error(exc: BaseException, expected: bool) -> None:
    assert is_connection_error(exc) is expected


def test_is_connection_error_follows_cause_chain() -> None:
    from pydantic_ai.exceptions import ModelRetry

    wrapped = ModelRetry("Session terminated")
    wrapped.__cause__ = _session_terminated()
    assert is_connection_error(wrapped)
    plain = ModelRetry("Session terminated")  # a model-facing message alone proves nothing
    assert not is_connection_error(plain)


async def test_dropped_session_reconnects_once_and_retries_the_call(started: Any) -> None:
    conn, factory = started
    first = factory.built[0]

    async def _dead(*args: Any, **kwargs: Any) -> Any:
        raise _session_terminated()

    first.client.call_tool = _dead  # type: ignore[method-assign]
    agent: Agent[None, str] = Agent(
        _model_calling("mcp_demo_echo", {"text": "again"}), toolsets=[build_server_toolset(conn)]
    )
    result = await agent.run("go")
    assert "echo:again" in result.output
    assert len(factory.built) == 2, "exactly one reconnect"
    assert not first.is_running, "the dead connection was closed"
    # and the following turn simply reuses the new connection
    again = await Agent(
        _model_calling("mcp_demo_echo", {"text": "third"}), toolsets=[build_server_toolset(conn)]
    ).run("go")
    assert "echo:third" in again.output and len(factory.built) == 2


async def test_listing_after_dropped_session_reconnects() -> None:
    conn, factory = _connection()
    assert await conn.start()
    first = factory.built[0]

    async def _dead(*args: Any, **kwargs: Any) -> Any:
        raise anyio.ClosedResourceError()

    first.cache_tools = False
    first.client.list_tools = _dead  # type: ignore[method-assign]
    assert len(await _offered(build_server_toolset(conn))) == 5 and len(factory.built) == 2
    await conn.stop()


async def test_persistent_connection_failure_is_not_retried_forever(started: Any) -> None:
    conn, factory = started

    class _AlwaysDead(_Factory):
        def __call__(self, entry: McpServerEntry, connect: float, call: float) -> MCPToolset[Any]:
            ts = super().__call__(entry, connect, call)

            async def _dead(*args: Any, **kwargs: Any) -> Any:
                raise _session_terminated()

            ts.client.call_tool = _dead  # type: ignore[method-assign]
            return ts

    dead = _AlwaysDead()
    conn2 = McpConnection(_entry(), factory=dead)
    assert await conn2.start()
    agent: Agent[None, str] = Agent(
        _model_calling("mcp_demo_echo", {"text": "x"}), toolsets=[build_server_toolset(conn2)]
    )
    result = await agent.run("go")
    assert "RESULT" in result.output
    assert len(dead.built) == 2, "one reconnect, then the failure goes back to the model"
    await conn2.stop()


# --- confirmation -------------------------------------------------------------------------


def test_confirmation_decision() -> None:
    destructive = {"annotations": {"destructiveHint": True, "readOnlyHint": False}}
    read_only_destructive = {"annotations": {"destructiveHint": True, "readOnlyHint": True}}
    harmless = {"annotations": {"readOnlyHint": True}}
    none_annotations = {"annotations": None}
    confirm = frozenset({"notify"})
    assert tool_requires_confirmation("update_thing", destructive, frozenset())
    assert tool_requires_confirmation("notify", harmless, confirm)
    assert tool_requires_confirmation("notify", None, confirm)
    assert not tool_requires_confirmation("odd", read_only_destructive, frozenset())
    assert not tool_requires_confirmation("echo", harmless, confirm)
    assert not tool_requires_confirmation("echo", none_annotations, confirm)
    assert not tool_requires_confirmation("echo", {}, confirm)
    assert not tool_requires_confirmation("echo", None, frozenset())


async def test_confirmation_gate_for_config_and_annotations(started: Any) -> None:
    conn, _ = started
    conn.entry = _entry(confirm_tools=["notify"])
    confirmed: list[tuple[str, str, dict[str, Any]]] = []

    async def confirm(ctx: RunContext[Any], server: str, tool: str, args: dict[str, Any]) -> None:
        confirmed.append((server, tool, args))

    toolset = build_server_toolset(conn, confirm=confirm)
    for tool, args in (
        ("mcp_demo_echo", {"text": "a"}),
        ("mcp_demo_update_thing", {"name": "n"}),
        ("mcp_demo_notify", {"name": "m"}),
        ("mcp_demo_odd", {"name": "o"}),
    ):
        await Agent(_model_calling(tool, args), toolsets=[toolset]).run("go")
    assert confirmed == [
        ("demo", "update_thing", {"name": "n"}),
        ("demo", "notify", {"name": "m"}),
    ]


async def test_denied_confirmation_is_reported_to_model_and_call_skipped(started: Any) -> None:
    conn, _ = started
    calls: list[str] = []

    async def deny(ctx: RunContext[Any], server: str, tool: str, args: dict[str, Any]) -> None:
        raise McpConfirmationDenied(server, tool)

    toolset = build_server_toolset(conn, confirm=deny)
    original = conn.call_tool

    async def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(args[0])
        return await original(*args, **kwargs)

    conn.call_tool = spy  # type: ignore[method-assign]
    result = await Agent(
        _model_calling("mcp_demo_update_thing", {"name": "n"}), toolsets=[toolset]
    ).run("go")
    assert "denied" in result.output and calls == []


async def test_confirmation_uses_turn_deps_notifier(started: Any) -> None:
    """End to end through check_confirmation: notifier gets the model-facing tool name."""
    conn, _ = started
    notified: list[tuple[str, str]] = []

    async def notifier(name: str, payload: str) -> None:
        notified.append((name, payload))

    deps = TurnDeps(writes_approved=[], seen_intent_ids=set(), tool_call_notifier=notifier)
    agent: Agent[TurnDeps, str] = Agent(
        _model_calling("mcp_demo_update_thing", {"name": "n"}),
        deps_type=TurnDeps,
        toolsets=[build_server_toolset(conn)],
    )
    result = await agent.run("go", deps=deps)
    assert "updated:n" in result.output
    assert notified[0][0] == "mcp_demo_update_thing"
    assert json.loads(notified[0][1])["arguments"] == {"name": "n"}


async def test_call_to_unavailable_server_is_tool_failed() -> None:
    conn = McpConnection(_entry(), factory=_Factory(), clock=lambda: 0.0)
    conn._retry_at = 1e9  # inside the backoff window
    with pytest.raises(ToolFailed, match="unavailable"):
        await conn._run(lambda ts: ts.list_tools())


# --- idle timeouts of long-lived HTTP streams ----------------------------------------------


def test_sse_stream_idle_timeout_is_not_the_call_timeout() -> None:
    entry = McpServerEntry(id="s", transport="sse", url="http://localhost/sse")
    ts = build_mcp_toolset(entry, 10.0, 30.0)
    assert ts.client.transport.sse_read_timeout.total_seconds() > 3600  # type: ignore[union-attr]


async def test_streamable_http_client_read_timeout_ignores_call_timeout() -> None:
    entry = McpServerEntry(id="h", transport="streamable-http", url="http://localhost/mcp")
    ts = build_mcp_toolset(entry, 10.0, 30.0)
    factory = ts.client.transport.httpx_client_factory  # type: ignore[union-attr]
    # fastmcp passes Timeout(30, read=call_seconds); the factory must not honour the read part
    async with factory(headers={}, timeout=httpx2.Timeout(30.0, read=30.0)) as http:
        assert http.timeout.read is not None and http.timeout.read > 3600
