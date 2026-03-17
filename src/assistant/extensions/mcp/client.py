"""
Component ID: CMP_TOOL_RUNTIME_REGISTRY

MCP client session lifecycle: connect via SSE, list tools, call tool.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import structlog
from mcp import ClientSession
from mcp.client.sse import sse_client

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


async def call_mcp_tool(
    url: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    connect_timeout: float = 10.0,
    call_timeout: float = 30.0,
) -> dict[str, Any]:
    """Invoke MCP tool and return normalized result.

    Returns dict with keys: status, content, error, is_error.
    """
    try:
        async with mcp_sse_session(
            url,
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
            url=url[:50] + "..." if len(url) > 50 else url,
            error=str(exc),
        )
        return {
            "status": "error",
            "content": None,
            "error": str(exc),
            "is_error": True,
        }
