"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP schema cache: authoritative tool specs (input schema + description) fetched
from servers via list_tools.

Hand-written input_schema entries in tool_map.yaml drift from the real server
signatures, so the bridge prefers the schema the server itself advertises (and,
for tools that opt in, its description).
Schemas are fetched once at startup (and lazily for servers that were not
reachable then) and served synchronously to the bridge. A server whose refresh
failed is retried with exponential backoff rather than on every tool build.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import structlog

from assistant.extensions.mcp.session_pool import McpSessionPool

logger = structlog.get_logger(__name__)

# Backoff before re-trying a server whose refresh failed: doubles per failure.
_RETRY_BASE_SECONDS = 30.0
_RETRY_MAX_SECONDS = 600.0


@dataclass(frozen=True)
class McpToolSpec:
    """Tool spec as advertised by an MCP server."""

    input_schema: dict[str, Any] | None
    description: str | None


class McpSchemaCache:
    """In-memory cache of {server_id: {tool_name: McpToolSpec}}."""

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, McpToolSpec]] = {}
        self._pending: dict[str, asyncio.Task[None]] = {}
        self._generation = 0
        # server_id -> (consecutive failures, monotonic time before which not to retry)
        self._failures: dict[str, tuple[int, float]] = {}

    @property
    def generation(self) -> int:
        """Bumped whenever cached specs change; lets consumers drop tools built earlier."""
        return self._generation

    def get(self, server_id: str, tool_name: str) -> McpToolSpec | None:
        return self._schemas.get(server_id, {}).get(tool_name)

    def has_server(self, server_id: str) -> bool:
        return server_id in self._schemas

    def set_server(self, server_id: str, specs: dict[str, McpToolSpec]) -> None:
        self._schemas[server_id] = specs
        self._generation += 1

    def clear(self) -> None:
        self._schemas.clear()
        self._generation += 1

    async def refresh_server(
        self,
        server_id: str,
        connection: dict[str, Any],
        pool: McpSessionPool,
        *,
        connect_timeout: float,
        call_timeout: float,
    ) -> None:
        """Fetch tool schemas from one server. Failures are logged, never raised."""
        try:
            async with pool.acquire(
                transport=connection.get("transport", "sse"),
                url=connection.get("url") or None,
                command=connection.get("command") or None,
                args=connection.get("args") or None,
                env=connection.get("env") or None,
                connect_timeout=connect_timeout,
                call_timeout=call_timeout,
            ) as session:
                result = await asyncio.wait_for(session.list_tools(), timeout=call_timeout)
        except Exception as exc:
            failures = self._failures.get(server_id, (0, 0.0))[0] + 1
            delay = min(_RETRY_BASE_SECONDS * 2 ** (failures - 1), _RETRY_MAX_SECONDS)
            self._failures[server_id] = (failures, time.monotonic() + delay)
            logger.warning(
                "mcp.schema_cache.refresh_failed",
                server_id=server_id,
                error=str(exc),
                failures=failures,
                retry_in_seconds=delay,
            )
            return
        self._failures.pop(server_id, None)
        specs = {
            tool.name: McpToolSpec(
                input_schema=tool.inputSchema or None,
                description=(tool.description or "").strip() or None,
            )
            for tool in result.tools
        }
        self.set_server(server_id, specs)
        logger.info("mcp.schema_cache.refreshed", server_id=server_id, tools=len(specs))

    def schedule_refresh(
        self,
        server_id: str,
        connection: dict[str, Any],
        pool: McpSessionPool,
        *,
        connect_timeout: float,
        call_timeout: float,
    ) -> None:
        """Fire-and-forget refresh for a server missing from the cache.

        No-op outside a running event loop, when a refresh is already in flight,
        or while the server is backing off after a failed refresh.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        failure = self._failures.get(server_id)
        if failure is not None and time.monotonic() < failure[1]:
            return
        self._start_refresh(
            server_id,
            connection,
            pool,
            connect_timeout=connect_timeout,
            call_timeout=call_timeout,
        )

    async def ensure_refreshed(
        self,
        server_id: str,
        connection: dict[str, Any],
        pool: McpSessionPool,
        *,
        connect_timeout: float,
        call_timeout: float,
    ) -> None:
        """Refresh one server, joining an in-flight refresh instead of starting another."""
        await self._start_refresh(
            server_id,
            connection,
            pool,
            connect_timeout=connect_timeout,
            call_timeout=call_timeout,
        )

    async def cancel_pending(self) -> None:
        """Cancel and await in-flight refreshes (call before closing the session pool)."""
        pending = [task for task in self._pending.values() if not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._pending.clear()

    def _start_refresh(
        self,
        server_id: str,
        connection: dict[str, Any],
        pool: McpSessionPool,
        *,
        connect_timeout: float,
        call_timeout: float,
    ) -> asyncio.Task[None]:
        pending = self._pending.get(server_id)
        if pending is not None and not pending.done():
            return pending
        task = asyncio.get_running_loop().create_task(
            self.refresh_server(
                server_id,
                connection,
                pool,
                connect_timeout=connect_timeout,
                call_timeout=call_timeout,
            )
        )
        self._pending[server_id] = task
        return task
