"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP session pool: reuses connections across tool calls to avoid per-call reconnection overhead.

Usage:
    pool = McpSessionPool()
    async with pool.acquire(transport="sse", url="http://...") as session:
        result = await session.call_tool(...)
    await pool.close()  # on shutdown
"""

import asyncio
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import structlog
from mcp import ClientSession

from assistant.extensions.mcp.client import (
    mcp_sse_session,
    mcp_stdio_session,
    mcp_streamable_http_session,
)

logger = structlog.get_logger(__name__)

# Default idle TTL before evicting a cached session (seconds)
_DEFAULT_IDLE_TTL = 300.0
# Maximum sessions per pool
_DEFAULT_MAX_SESSIONS = 20


@dataclass
class _PoolEntry:
    """Internal: wraps a cached MCP session."""

    session: ClientSession
    key: str
    last_used: float = field(default_factory=time.monotonic)
    in_use: bool = False
    # Context manager stack for cleanup
    _cleanup: Any = None


def _session_key(
    transport: str,
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
) -> str:
    """Derive a unique cache key for a connection."""
    if transport == "stdio":
        return f"stdio:{command}:{','.join(args or [])}"
    return f"{transport}:{url}"


class McpSessionPool:
    """Connection pool for MCP sessions.

    Reuses idle sessions when available, creates new ones on demand.
    Thread-safe via asyncio.Lock.
    """

    def __init__(
        self,
        idle_ttl: float = _DEFAULT_IDLE_TTL,
        max_sessions: int = _DEFAULT_MAX_SESSIONS,
    ) -> None:
        self._pool: dict[str, list[_PoolEntry]] = {}
        self._lock = asyncio.Lock()
        self._idle_ttl = idle_ttl
        self._max_sessions = max_sessions
        self._total_sessions = 0
        self._sweeper_task: asyncio.Task[None] | None = None
        self._sweeper_interval: float = 60.0

    @asynccontextmanager
    async def acquire(
        self,
        *,
        transport: str = "sse",
        url: str | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        connect_timeout: float = 10.0,
        call_timeout: float = 30.0,
    ) -> AsyncGenerator[ClientSession, None]:
        """Acquire a session from the pool or create a new one.

        The session is returned to the pool when the context exits normally.
        On error, the session is discarded.
        """
        key = _session_key(transport, url, command, args)
        entry = await self._try_reuse(key)

        if entry is not None:
            logger.debug("mcp.session_pool.reused", key=key)
            try:
                yield entry.session
                await self._release(entry)
            except Exception:
                await self._discard(entry)
                raise
            return

        # Create new session — use the raw context managers from client.py
        # We need to manage the context manually for pooling
        logger.debug("mcp.session_pool.new_session", key=key, transport=transport)
        if transport == "sse":
            ctx = mcp_sse_session(
                url or "", connect_timeout=connect_timeout, call_timeout=call_timeout
            )
        elif transport == "stdio":
            ctx = mcp_stdio_session(command or "", args=args, env=env)
        elif transport == "streamable-http":
            ctx = mcp_streamable_http_session(url or "", connect_timeout=connect_timeout)
        else:
            raise ValueError(f"Unsupported transport: {transport!r}")

        session = await ctx.__aenter__()
        entry = _PoolEntry(session=session, key=key, _cleanup=ctx)
        async with self._lock:
            self._total_sessions += 1
        try:
            yield session
            await self._release(entry)
        except Exception:
            await self._discard(entry)
            raise

    async def _try_reuse(self, key: str) -> _PoolEntry | None:
        """Try to find an idle session for the given key."""
        async with self._lock:
            entries = self._pool.get(key, [])
            now = time.monotonic()
            # Find first idle, non-expired entry
            for entry in entries:
                if not entry.in_use and (now - entry.last_used) < self._idle_ttl:
                    entry.in_use = True
                    entry.last_used = now
                    return entry
            # Evict expired entries
            fresh = [e for e in entries if e.in_use or (now - e.last_used) < self._idle_ttl]
            stale = [e for e in entries if not e.in_use and (now - e.last_used) >= self._idle_ttl]
            self._pool[key] = fresh
            self._total_sessions -= len(stale)
        # Clean up stale sessions outside lock
        for entry in stale:
            await self._close_entry(entry)
        return None

    async def _release(self, entry: _PoolEntry) -> None:
        """Return a session to the pool."""
        async with self._lock:
            entry.in_use = False
            entry.last_used = time.monotonic()
            if self._total_sessions > self._max_sessions:
                # Over capacity — discard instead
                self._total_sessions -= 1
                await self._close_entry(entry)
                return
            bucket = self._pool.setdefault(entry.key, [])
            if entry not in bucket:
                bucket.append(entry)

    async def _discard(self, entry: _PoolEntry) -> None:
        """Discard a session (on error)."""
        async with self._lock:
            bucket = self._pool.get(entry.key, [])
            if entry in bucket:
                bucket.remove(entry)
            self._total_sessions -= 1
        await self._close_entry(entry)

    async def _close_entry(self, entry: _PoolEntry) -> None:
        """Close the underlying session context manager."""
        if entry._cleanup is not None:
            try:
                await entry._cleanup.__aexit__(None, None, None)
            except Exception:
                logger.debug("mcp.session_pool.close_error", key=entry.key, exc_info=True)

    async def sweep(self) -> int:
        """Evict all idle sessions that have exceeded idle_ttl. Returns count evicted."""
        now = time.monotonic()
        stale: list[_PoolEntry] = []
        async with self._lock:
            for key, entries in list(self._pool.items()):
                fresh = [e for e in entries if e.in_use or (now - e.last_used) < self._idle_ttl]
                evicted = [e for e in entries if not e.in_use and (now - e.last_used) >= self._idle_ttl]
                stale.extend(evicted)
                self._pool[key] = fresh
                self._total_sessions -= len(evicted)
        for entry in stale:
            await self._close_entry(entry)
        if stale:
            logger.info("mcp.session_pool.sweep", evicted=len(stale))
        return len(stale)

    async def start_sweeper(self, interval: float = 60.0) -> None:
        """Start a background task that sweeps expired sessions every `interval` seconds."""
        if self._sweeper_task is not None:
            return
        self._sweeper_interval = interval
        self._sweeper_task = asyncio.create_task(self._sweep_loop())
        logger.info("mcp.session_pool.sweeper_started", interval_seconds=interval, idle_ttl=self._idle_ttl)

    async def stop_sweeper(self) -> None:
        """Cancel the background sweeper task if running."""
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._sweeper_task
            self._sweeper_task = None
            logger.info("mcp.session_pool.sweeper_stopped")

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(self._sweeper_interval)
            await self.sweep()

    async def close(self) -> None:
        """Close all pooled sessions and stop the sweeper. Call on application shutdown."""
        await self.stop_sweeper()
        async with self._lock:
            all_entries = [e for bucket in self._pool.values() for e in bucket]
            self._pool.clear()
            self._total_sessions = 0
        for entry in all_entries:
            await self._close_entry(entry)
        logger.info("mcp.session_pool.closed", sessions_closed=len(all_entries))

    @property
    def active_count(self) -> int:
        """Number of sessions currently in the pool (including in-use)."""
        return self._total_sessions
