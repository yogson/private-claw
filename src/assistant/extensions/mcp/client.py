"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP client session lifecycle: connect via SSE, stdio, or streamable-HTTP, then call tools.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import structlog
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def mcp_sse_session(
    url: str,
    *,
    connect_timeout: float = 10.0,
    call_timeout: float = 30.0,
) -> AsyncGenerator[ClientSession, None]:
    """Create MCP client session over SSE transport."""
    async with (
        sse_client(
            url,
            timeout=connect_timeout,
            sse_read_timeout=call_timeout,
        ) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


@asynccontextmanager
async def mcp_stdio_session(
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> AsyncGenerator[ClientSession, None]:
    """Create MCP client session over stdio transport.

    Spawns the server as a subprocess and communicates via stdin/stdout.
    """
    server_params = StdioServerParameters(
        command=command,
        args=args or [],
        env=env,
    )
    async with (
        stdio_client(server_params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


@asynccontextmanager
async def mcp_streamable_http_session(
    url: str,
    *,
    connect_timeout: float = 10.0,
) -> AsyncGenerator[ClientSession, None]:
    """Create MCP client session over streamable-HTTP transport."""
    async with (
        streamablehttp_client(
            url,
            timeout=connect_timeout,
        ) as (read_stream, write_stream, _),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


@asynccontextmanager
async def mcp_session(
    *,
    transport: str = "sse",
    url: str | None = None,
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    connect_timeout: float = 10.0,
    call_timeout: float = 30.0,
) -> AsyncGenerator[ClientSession, None]:
    """Unified session factory dispatching to the appropriate transport.

    Args:
        transport: One of "sse", "stdio", "streamable-http".
        url: Required for sse and streamable-http transports.
        command: Required for stdio transport (the executable to spawn).
        args: Optional args for stdio transport.
        env: Optional env vars for stdio transport.
        connect_timeout: Connection timeout in seconds.
        call_timeout: Call/read timeout in seconds.
    """
    if transport == "sse":
        if not url:
            raise ValueError("url is required for SSE transport")
        async with mcp_sse_session(
            url, connect_timeout=connect_timeout, call_timeout=call_timeout
        ) as session:
            yield session
    elif transport == "stdio":
        if not command:
            raise ValueError("command is required for stdio transport")
        async with mcp_stdio_session(command, args=args, env=env) as session:
            yield session
    elif transport == "streamable-http":
        if not url:
            raise ValueError("url is required for streamable-http transport")
        async with mcp_streamable_http_session(url, connect_timeout=connect_timeout) as session:
            yield session
    else:
        raise ValueError(f"Unsupported MCP transport: {transport!r}")


async def call_mcp_tool(
    url: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    transport: str = "sse",
    command: str | None = None,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    connect_timeout: float = 10.0,
    call_timeout: float = 30.0,
) -> dict[str, Any]:
    """Invoke MCP tool and return normalized result.

    Returns dict with keys: status, content, error, is_error.
    """
    try:
        async with mcp_session(
            transport=transport,
            url=url,
            command=command,
            args=args,
            env=env,
            connect_timeout=connect_timeout,
            call_timeout=call_timeout,
        ) as session:
            result = await session.call_tool(
                tool_name,
                arguments or {},
                read_timeout_seconds=timedelta(seconds=call_timeout),
            )
            if result.isError:
                return {
                    "status": "error",
                    "content": None,
                    "error": str(result.content) if result.content else "Unknown MCP error",
                    "is_error": True,
                }
            parts = []
            if result.content:
                for block in result.content:
                    if hasattr(block, "text") and block.text:
                        parts.append(block.text)
            return {
                "status": "ok",
                "content": "\n".join(parts) if parts else None,
                "error": None,
                "is_error": False,
            }
    except Exception as exc:
        logger.warning(
            "mcp.call_failed",
            tool_name=tool_name,
            transport=transport,
            url=url[:50] + "..." if url and len(url) > 50 else url,
            error=str(exc),
        )
        return {
            "status": "error",
            "content": None,
            "error": str(exc),
            "is_error": True,
        }
