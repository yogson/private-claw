"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

Native MCP integration on top of pydantic-ai's ``MCPToolset``.

Whole servers are connected and the agent discovers their tools the way standard MCP
clients do. This module owns:

* one long-lived connection per enabled server (``McpConnection``), entered once at app
  startup and shared by every agent / capability set, so stdio servers are not respawned
  per turn;
* lazy connect with backoff for servers that were down at startup, and self-healing when
  a long-lived session dies mid-flight: the connection is re-established, listing
  operations are retried once, but a tool *call* is never replayed (the server may already
  have executed it) - the model gets a tool error saying the outcome is unknown;
* in-place reconfiguration when a server's config changes, so cached agents keep working
  against the current connection while the old session is closed once it is idle;
* the per-capability toolset stack: argument unwrapping, "notify before call" hook,
  ``include_tools`` / ``exclude_tools`` filter and the ``mcp_<server_id>_`` name prefix.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

import anyio
import httpx2
import structlog
from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport
from fastmcp.exceptions import McpError  # type: ignore[attr-defined]
from mcp.shared._httpx_utils import MCP_DEFAULT_TIMEOUT, create_mcp_http_client
from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.exceptions import ModelRetry, ToolFailed
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import InstructionPart
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset
from pydantic_ai.toolsets.abstract import ToolsetTool

from assistant.core.config.schemas import McpServerEntry, McpServersConfig
from assistant.extensions.mcp.confirmation import McpConfirmationDenied, check_confirmation

logger = structlog.get_logger(__name__)

T = TypeVar("T")

# Every MCP tool is exposed to the model as ``mcp_<server_id>_<tool>``. No first-party
# tool name starts with this prefix (enforced by tests and by bootstrap validation), so
# it unambiguously identifies MCP tools.
MCP_TOOL_PREFIX = "mcp_"

_CONNECT_BACKOFF_BASE_SECONDS = 5.0
_CONNECT_BACKOFF_MAX_SECONDS = 300.0
_CLOSE_TIMEOUT_SECONDS = 5.0
_LOG_ERROR_MAX_CHARS = 300
# Transport-level idle limit of the long-lived HTTP streams (SSE stream, streamable-http
# listening stream). Connections are shared across turns, so they sit idle between them;
# this must stay far above any realistic gap. Per-call limits are separate (``call_seconds``
# -> the MCP session's ``read_timeout_seconds``).
_STREAM_IDLE_SECONDS = 24 * 3600.0

# Phrases of runtime errors raised by fastmcp / the MCP SDK for a session that is gone.
_DEAD_SESSION_MARKERS = (
    "session terminated",  # server answered 404 for our session id (restart / expiry)
    "connection closed",  # stdio server exited or stream closed
    "client is not connected",  # fastmcp client whose session task ended
    "client failed to connect",
)
_TRANSPORT_ERROR_BASES = frozenset({"TransportError", "NetworkError", "ProtocolError"})
_HTTP_MODULES = frozenset({"httpx", "httpx2", "httpcore"})


def mcp_tool_prefix(server_id: str) -> str:
    """Prefix (without trailing underscore) applied to a server's tools."""
    return f"{MCP_TOOL_PREFIX}{server_id}"


def is_mcp_tool_name(tool_name: str) -> bool:
    """True when ``tool_name`` is a model-facing MCP tool name."""
    return tool_name.startswith(MCP_TOOL_PREFIX)


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


def _leaves(exc: BaseException, _seen: set[int] | None = None) -> Iterator[BaseException]:
    """Yield ``exc`` plus everything reachable through ``__cause__`` and exception groups."""
    seen = _seen if _seen is not None else set()
    if id(exc) in seen:
        return
    seen.add(id(exc))
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaves(inner, seen)
        return
    yield exc
    if exc.__cause__ is not None:
        yield from _leaves(exc.__cause__, seen)


def _is_transport_error(exc: BaseException) -> bool:
    """httpx / httpcore network failure that is not a timeout."""
    mro = type(exc).__mro__
    names = {cls.__name__ for cls in mro}
    if names & {"TimeoutException", "TimeoutError"}:
        return False
    in_http_lib = any(cls.__module__.split(".")[0] in _HTTP_MODULES for cls in mro)
    return in_http_lib and bool(names & _TRANSPORT_ERROR_BASES)


def is_connection_error(exc: BaseException) -> bool:
    """Decide whether ``exc`` means the MCP session/transport is dead.

    Only failures of the connection itself count: closed or broken streams, refused or
    reset sockets, httpx transport errors (not timeouts), and the "session terminated" /
    "connection closed" / "client is not connected" errors of the MCP stack. Ordinary
    tool errors (``ToolError``, ``isError`` results), argument rejections, and timeouts
    return False, so they go back to the model instead of triggering a reconnect.

    pydantic-ai converts protocol-level errors into ``ModelRetry`` chained from the
    original ``McpError``; ``__cause__`` and exception groups are therefore inspected.
    """
    for err in _leaves(exc):
        if isinstance(
            err, anyio.ClosedResourceError | anyio.BrokenResourceError | anyio.EndOfStream
        ):
            return True
        if isinstance(err, ConnectionError):  # refused / reset / aborted / broken pipe
            return True
        if _is_transport_error(err):
            return True
        if isinstance(err, McpError | RuntimeError):
            text = str(err).lower()
            if any(marker in text for marker in _DEAD_SESSION_MARKERS):
                return True
    return False


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

ToolsetFactory = Callable[[McpServerEntry, float, float], MCPToolset[Any]]


def _idle_tolerant_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
    **_: Any,
) -> httpx2.AsyncClient:
    """httpx client whose read timeout does not follow the per-call timeout.

    fastmcp derives the HTTP read timeout from ``call_seconds``; for streamable-http that
    would also cut the idle server-to-client listening stream after ``call_seconds``.
    The incoming ``timeout`` is therefore ignored: connect/write/pool keep the MCP default
    and reads may idle. Per-call limits are still enforced by the MCP session.
    Extra kwargs (fastmcp passes ``follow_redirects=True``) are ignored, matching fastmcp's
    own no-factory path: the MCP transport follows same-origin redirects itself.
    """
    return create_mcp_http_client(
        headers=headers,
        timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=_STREAM_IDLE_SECONDS),
        auth=auth,
    )


def build_mcp_toolset(
    entry: McpServerEntry, connect_seconds: float, call_seconds: float
) -> MCPToolset[Any]:
    """Build the (not yet entered) ``MCPToolset`` for a server entry."""
    transport: StdioTransport | SSETransport | StreamableHttpTransport
    if entry.transport == "stdio":
        # keep_alive=False: leaving the toolset terminates the subprocess.
        transport = StdioTransport(
            command=entry.command,
            args=list(entry.args),
            env=dict(entry.env) or None,
            keep_alive=False,
        )
    elif entry.transport == "sse":
        transport = SSETransport(
            url=entry.url,
            headers=dict(entry.headers) or None,
            sse_read_timeout=_STREAM_IDLE_SECONDS,
        )
    else:
        transport = StreamableHttpTransport(
            url=entry.url,
            headers=dict(entry.headers) or None,
            httpx_client_factory=_idle_tolerant_http_client,
        )
    return MCPToolset(
        transport,
        id=entry.id,
        # A tool error is a result the model reads and adapts to ("failed"), not a retry
        # that eats the agent's (zero) tool-retry budget. Protocol-level rejections
        # still surface as a single ModelRetry.
        tool_error_behavior="failed",
        max_retries=1,
        include_instructions=entry.include_instructions,
        init_timeout=connect_seconds,
        read_timeout=call_seconds,
    )


class McpConnection(AbstractToolset[Any]):
    """Shared, self-healing connection to one MCP server.

    The app (lifespan) calls :meth:`start` once and :meth:`stop` on shutdown; agent runs
    entering and leaving the toolset are no-ops here, so a run never (re)spawns a server.
    A server that is down is retried lazily with exponential backoff when a turn needs
    it; during the backoff window its tools are simply absent.

    The object is stable per server id: :meth:`reconfigure` swaps the entry / timeouts in
    place and the next operation connects with the new settings. The superseded session is
    closed as soon as no operation is in flight, never in the middle of a call.
    """

    def __init__(
        self,
        entry: McpServerEntry,
        *,
        connect_seconds: float = 10.0,
        call_seconds: float = 30.0,
        factory: ToolsetFactory = build_mcp_toolset,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.entry = entry
        self._connect_seconds = connect_seconds
        self._call_seconds = call_seconds
        self._factory = factory
        self._clock = clock
        self._toolset: MCPToolset[Any] | None = None
        self._failures = 0
        self._retry_at = 0.0
        self._lock: asyncio.Lock | None = None
        self._inflight = 0
        self._stale: list[MCPToolset[Any]] = []  # superseded sessions awaiting an idle moment
        self._tasks: set[asyncio.Task[None]] = set()

    # -- AbstractToolset plumbing ------------------------------------------------

    @property
    def id(self) -> str | None:
        return self.entry.id

    @property
    def tool_name_conflict_hint(self) -> str:
        return f"MCP server '{self.entry.id}' tool names clash; filter them with exclude_tools."

    async def __aenter__(self) -> "McpConnection":
        return self  # the app owns the connection, not individual runs

    async def __aexit__(self, *args: Any) -> bool | None:
        return None

    @property
    def connected(self) -> bool:
        return self._toolset is not None

    # -- lifecycle ---------------------------------------------------------------

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def start(self) -> bool:
        """Connect now (honouring backoff). Never raises; returns whether connected."""
        return await self._ensure() is not None

    async def stop(self) -> None:
        """Close the connection and any superseded sessions (idempotent)."""
        async with self._get_lock():
            toolset, self._toolset = self._toolset, None
            stale, self._stale = self._stale, []
        for ts in (toolset, *stale):
            await self._close(ts)

    def reconfigure(
        self, entry: McpServerEntry, connect_seconds: float, call_seconds: float
    ) -> None:
        """Adopt a changed server config; the next operation reconnects with it.

        The live session is detached now and closed once nothing is in flight, so a call
        that is mid-flight completes on the old session.
        """
        self.entry = entry
        self._connect_seconds = connect_seconds
        self._call_seconds = call_seconds
        self._failures = 0
        self._retry_at = 0.0
        if self._toolset is not None:
            self._stale.append(self._toolset)
            self._toolset = None
        if self._stale and self._inflight == 0:
            try:
                task = asyncio.get_running_loop().create_task(self._close_stale())
            except RuntimeError:
                return  # no loop (sync caller): closed on the next idle moment / stop()
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _close_stale(self) -> None:
        """Close superseded sessions, unless an operation is (or becomes) in flight."""
        while self._stale and self._inflight == 0:
            await self._close(self._stale.pop())

    async def _close(self, toolset: MCPToolset[Any] | None) -> None:
        if toolset is None:
            return
        try:
            await asyncio.wait_for(toolset.__aexit__(None, None, None), _CLOSE_TIMEOUT_SECONDS)
        except Exception as exc:  # a dead session may fail to close cleanly
            logger.debug("mcp.close_failed", server_id=self.entry.id, error=str(exc)[:200])

    async def _ensure(self, *, force: bool = False) -> MCPToolset[Any] | None:
        async with self._get_lock():
            if self._toolset is not None:
                return self._toolset
            if not force and self._clock() < self._retry_at:
                return None
            toolset = self._factory(self.entry, self._connect_seconds, self._call_seconds)
            try:
                await asyncio.wait_for(toolset.__aenter__(), self._connect_seconds + 5.0)
            except Exception as exc:
                self._failures += 1
                backoff = min(
                    _CONNECT_BACKOFF_BASE_SECONDS * 2 ** (self._failures - 1),
                    _CONNECT_BACKOFF_MAX_SECONDS,
                )
                self._retry_at = self._clock() + backoff
                logger.warning(
                    "mcp.connect_failed",
                    server_id=self.entry.id,
                    transport=self.entry.transport,
                    attempt=self._failures,
                    retry_in_seconds=backoff,
                    error=str(exc)[:_LOG_ERROR_MAX_CHARS] or type(exc).__name__,
                )
                return None
            self._failures = 0
            self._retry_at = 0.0
            self._toolset = toolset
            logger.info("mcp.connected", server_id=self.entry.id, transport=self.entry.transport)
            return toolset

    async def _reconnect(self, failed: MCPToolset[Any]) -> MCPToolset[Any] | None:
        """Replace ``failed`` with a fresh connection (once, even if racing callers)."""
        async with self._get_lock():
            current = self._toolset
            if current is failed:
                self._toolset = None
        if current is failed:
            await self._close(failed)
        return await self._ensure(force=True)

    def _tool_error(self, toolset: MCPToolset[Any], message: str) -> Exception:
        """Error the model reads as a tool result, matching the toolset's error behaviour."""
        if toolset.tool_error_behavior == "retry":
            return ModelRetry(message)
        return ToolFailed(message)

    async def _run(
        self, op: Callable[[MCPToolset[Any]], Awaitable[T]], *, replay: bool = True
    ) -> T:
        """Run ``op`` on the live session, healing the connection if it turns out dead.

        ``replay=True`` (idempotent listing operations): reconnect and run ``op`` once more.
        ``replay=False`` (tool calls): reconnect so the next operation works, but do not
        run ``op`` again - the server may already have executed it - and report that the
        outcome is unknown.
        """
        toolset = await self._ensure()
        if toolset is None:
            raise ToolFailed(f"MCP server '{self.entry.id}' is currently unavailable.")
        self._inflight += 1
        try:
            try:
                return await op(toolset)
            except Exception as exc:
                if not is_connection_error(exc):
                    raise
                logger.warning(
                    "mcp.session_lost",
                    server_id=self.entry.id,
                    error=str(exc)[:_LOG_ERROR_MAX_CHARS] or type(exc).__name__,
                )
                fresh = await self._reconnect(toolset)
                if not replay:
                    raise self._tool_error(
                        toolset,
                        f"Connection to MCP server '{self.entry.id}' was lost during the call; "
                        "the tool may or may not have executed. Check state before retrying.",
                    ) from exc
                if fresh is None:
                    raise ToolFailed(f"MCP server '{self.entry.id}' is unreachable.") from exc
                logger.info("mcp.reconnected", server_id=self.entry.id)
                return await op(fresh)
        finally:
            self._inflight -= 1
            if self._inflight == 0 and self._stale:
                await self._close_stale()

    # -- toolset API -------------------------------------------------------------

    async def get_instructions(
        self, ctx: RunContext[Any]
    ) -> str | InstructionPart | Sequence[str | InstructionPart] | None:
        # Ensure the connection first (honouring backoff): on the first turn after startup,
        # or for a lazily connected server, nothing is connected yet and the instructions
        # would otherwise be silently dropped.
        try:
            return await self._run(lambda ts: ts.get_instructions(ctx))
        except Exception as exc:
            logger.debug(
                "mcp.instructions_unavailable",
                server_id=self.entry.id,
                error=str(exc)[:_LOG_ERROR_MAX_CHARS] or type(exc).__name__,
            )
            return None

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        try:
            tools = await self._run(lambda ts: ts.get_tools(ctx))
        except Exception as exc:
            # An unavailable server must not break the turn: it just contributes no tools.
            logger.warning(
                "mcp.list_tools_failed",
                server_id=self.entry.id,
                error=str(exc)[:_LOG_ERROR_MAX_CHARS] or type(exc).__name__,
            )
            return {}
        return {name: replace(tool, toolset=self) for name, tool in tools.items()}

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        async def _call(ts: MCPToolset[Any]) -> Any:
            return await ts.call_tool(name, tool_args, ctx, replace(tool, toolset=ts))

        try:
            result = await self._run(_call, replay=False)  # never replay a call
        except Exception as exc:
            logger.info(
                "mcp.tool_invoked",
                mcp_server=self.entry.id,
                mcp_tool=name,
                status="error",
                error=str(exc)[:_LOG_ERROR_MAX_CHARS],
                arg_names=sorted(tool_args),
            )
            raise
        logger.info("mcp.tool_invoked", mcp_server=self.entry.id, mcp_tool=name, status="ok")
        return result


# ---------------------------------------------------------------------------
# Confirmation
# ---------------------------------------------------------------------------


def tool_requires_confirmation(
    tool_name: str, metadata: dict[str, Any] | None, confirm_tools: frozenset[str]
) -> bool:
    """Decide whether a call needs user confirmation.

    True for tools listed in ``confirm_tools`` and for tools whose MCP annotations carry
    ``destructiveHint: true`` without ``readOnlyHint: true``. ``metadata`` is the tool
    definition's metadata; pydantic-ai stores the camelCase MCP annotations under
    ``metadata['annotations']``.
    """
    if tool_name in confirm_tools:
        return True
    annotations = (metadata or {}).get("annotations") or {}
    return (
        annotations.get("destructiveHint") is True and annotations.get("readOnlyHint") is not True
    )


ConfirmFunc = Callable[[RunContext[Any], str, str, dict[str, Any]], Awaitable[None]]


def unwrap_arguments(tool_args: dict[str, Any], schema: dict[str, Any] | None) -> dict[str, Any]:
    """Undo a common model mistake: wrapping all args as ``{"arguments": {...}}``.

    Unwrapped only when that is the sole argument and the tool has no ``arguments``
    property of its own.
    """
    inner = tool_args.get("arguments")
    if (
        len(tool_args) == 1
        and isinstance(inner, dict)
        and "arguments" not in ((schema or {}).get("properties") or {})
    ):
        return inner
    return tool_args


@dataclass
class McpConfirmationToolset(WrapperToolset[Any]):
    """Argument unwrapping plus the "notify before call" hook for sensitive tools.

    Calls to ``confirm_tools`` / destructive tools go through ``confirm`` first (by default
    :func:`check_confirmation`, which only notifies; it does not block today). If ``confirm``
    raises :class:`McpConfirmationDenied` the call is skipped and the model sees a tool error.

    With ``connection`` set, ``confirm_tools`` is read from the connection's current entry at
    call time, so cached agents follow config changes.
    """

    server_id: str = ""
    confirm_tools: frozenset[str] = field(default_factory=frozenset)
    confirm: ConfirmFunc = check_confirmation
    connection: "McpConnection | None" = None

    def _confirm_tools(self) -> frozenset[str]:
        if self.connection is not None:
            return frozenset(self.connection.entry.confirm_tools)
        return self.confirm_tools

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[Any], tool: ToolsetTool[Any]
    ) -> Any:
        tool_args = unwrap_arguments(tool_args, tool.tool_def.parameters_json_schema)
        if tool_requires_confirmation(name, tool.tool_def.metadata, self._confirm_tools()):
            try:
                await self.confirm(ctx, self.server_id, name, tool_args)
            except McpConfirmationDenied as exc:
                raise ToolFailed(str(exc)) from exc
        return await super().call_tool(name, tool_args, ctx, tool)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _tool_filter(
    connection: McpConnection,
) -> Callable[[RunContext[Any], ToolDefinition], bool]:
    """Include/exclude filter that reads the connection's *current* entry on every listing."""

    def _allowed(_ctx: RunContext[Any], tool_def: ToolDefinition) -> bool:
        entry = connection.entry
        if entry.include_tools is not None:
            return tool_def.name in entry.include_tools
        if entry.exclude_tools is not None:
            return tool_def.name not in entry.exclude_tools
        return True

    return _allowed


def build_server_toolset(
    connection: McpConnection, *, confirm: ConfirmFunc = check_confirmation
) -> AbstractToolset[Any]:
    """Wrap a shared connection: unwrapping + notify hook, include/exclude filter, prefix.

    Per-entry settings are resolved from ``connection.entry`` at call time, so an agent
    cached before a config change keeps following the live config.
    """
    toolset: AbstractToolset[Any] = McpConfirmationToolset(
        connection,
        server_id=connection.entry.id,
        confirm=confirm,
        connection=connection,
    )
    toolset = toolset.filtered(_tool_filter(connection))
    return toolset.prefixed(mcp_tool_prefix(connection.entry.id))


def _fingerprint(entry: McpServerEntry, connect_seconds: float, call_seconds: float) -> str:
    return f"{entry.model_dump_json()}|{connect_seconds}|{call_seconds}"


class McpToolsets:
    """Registry of the shared per-server connections, built from ``mcp_servers.yaml``."""

    def __init__(self, factory: ToolsetFactory = build_mcp_toolset) -> None:
        self._factory = factory
        self._connections: dict[str, tuple[str, McpConnection]] = {}

    def _connection(self, config: McpServersConfig, server_id: str) -> McpConnection | None:
        entry = next((s for s in config.servers if s.id == server_id and s.enabled), None)
        if entry is None:
            return None
        connect = float(config.timeouts.connect_seconds)
        call = float(entry.call_seconds or config.timeouts.call_seconds)
        fingerprint = _fingerprint(entry, connect, call)
        existing = self._connections.get(server_id)
        if existing is not None:
            if existing[0] == fingerprint:
                return existing[1]
            # Config changed: keep the same connection object (cached agents hold it) and
            # reconnect in place; the old session is closed once it is idle.
            existing[1].reconfigure(entry, connect, call)
            self._connections[server_id] = (fingerprint, existing[1])
            return existing[1]
        connection = McpConnection(
            entry, connect_seconds=connect, call_seconds=call, factory=self._factory
        )
        self._connections[server_id] = (fingerprint, connection)
        return connection

    def toolsets_for(
        self, config: McpServersConfig, server_ids: Sequence[str]
    ) -> list[AbstractToolset[Any]]:
        """Toolsets for the given server ids; unknown / disabled ids are skipped."""
        result: list[AbstractToolset[Any]] = []
        for server_id in server_ids:
            connection = self._connection(config, server_id)
            if connection is None:
                logger.debug("mcp.toolset_skipped", server_id=server_id)
                continue
            result.append(build_server_toolset(connection))
        return result

    async def start(self, config: McpServersConfig, server_ids: Sequence[str]) -> dict[str, bool]:
        """Connect the given servers concurrently; failures are logged, never raised."""
        connections = [c for sid in server_ids if (c := self._connection(config, sid)) is not None]
        results = await asyncio.gather(*(c.start() for c in connections))
        return {c.entry.id: ok for c, ok in zip(connections, results, strict=True)}

    async def close(self) -> None:
        connections = [c for _, c in self._connections.values()]
        self._connections.clear()
        await asyncio.gather(*(c.stop() for c in connections))


mcp_toolsets = McpToolsets()
