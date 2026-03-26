"""Tests for MCP client transport dispatch (P6)."""

import pytest

from assistant.extensions.mcp.client import mcp_session


@pytest.mark.asyncio
async def test_mcp_session_sse_requires_url() -> None:
    """SSE transport requires a url."""
    with pytest.raises(ValueError, match="url is required"):
        async with mcp_session(transport="sse", url=None):
            pass


@pytest.mark.asyncio
async def test_mcp_session_stdio_requires_command() -> None:
    """stdio transport requires a command."""
    with pytest.raises(ValueError, match="command is required"):
        async with mcp_session(transport="stdio", command=None):
            pass


@pytest.mark.asyncio
async def test_mcp_session_streamable_http_requires_url() -> None:
    """streamable-http transport requires a url."""
    with pytest.raises(ValueError, match="url is required"):
        async with mcp_session(transport="streamable-http", url=None):
            pass


@pytest.mark.asyncio
async def test_mcp_session_unsupported_transport() -> None:
    """Unknown transport raises ValueError."""
    with pytest.raises(ValueError, match="Unsupported MCP transport"):
        async with mcp_session(transport="websocket"):
            pass
