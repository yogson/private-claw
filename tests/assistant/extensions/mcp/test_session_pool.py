"""Tests for MCP session pool (P7)."""

import pytest

from assistant.extensions.mcp.session_pool import McpSessionPool, _session_key


def test_session_key_sse() -> None:
    assert _session_key("sse", url="http://localhost:9222/sse") == "sse:http://localhost:9222/sse"


def test_session_key_stdio() -> None:
    key = _session_key("stdio", command="npx", args=["-y", "server"])
    assert key == "stdio:npx:-y,server"


def test_session_key_streamable_http() -> None:
    key = _session_key("streamable-http", url="http://localhost:8080/mcp")
    assert key == "streamable-http:http://localhost:8080/mcp"


@pytest.mark.asyncio
async def test_pool_close_empty() -> None:
    """Closing an empty pool does not error."""
    pool = McpSessionPool()
    await pool.close()
    assert pool.active_count == 0


@pytest.mark.asyncio
async def test_pool_initial_state() -> None:
    pool = McpSessionPool(idle_ttl=60, max_sessions=5)
    assert pool.active_count == 0


@pytest.mark.asyncio
async def test_sweep_empty_pool() -> None:
    pool = McpSessionPool(idle_ttl=60)
    evicted = await pool.sweep()
    assert evicted == 0


@pytest.mark.asyncio
async def test_start_stop_sweeper() -> None:
    pool = McpSessionPool(idle_ttl=60)
    assert pool._sweeper_task is None
    await pool.start_sweeper(interval=3600.0)
    assert pool._sweeper_task is not None
    assert not pool._sweeper_task.done()
    await pool.stop_sweeper()
    assert pool._sweeper_task is None


@pytest.mark.asyncio
async def test_start_sweeper_idempotent() -> None:
    """Calling start_sweeper twice does not create a second task."""
    pool = McpSessionPool(idle_ttl=60)
    await pool.start_sweeper(interval=3600.0)
    task1 = pool._sweeper_task
    await pool.start_sweeper(interval=3600.0)
    assert pool._sweeper_task is task1
    await pool.stop_sweeper()


@pytest.mark.asyncio
async def test_close_stops_sweeper() -> None:
    pool = McpSessionPool(idle_ttl=60)
    await pool.start_sweeper(interval=3600.0)
    assert pool._sweeper_task is not None
    await pool.close()
    assert pool._sweeper_task is None
