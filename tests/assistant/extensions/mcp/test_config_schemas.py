"""Tests for MCP config schema transport validation (P6)."""

import pytest
from pydantic import ValidationError

from assistant.core.config.schemas import McpServerEntry


def test_sse_server_requires_url() -> None:
    with pytest.raises(ValidationError, match="url is required"):
        McpServerEntry(id="test", transport="sse")


def test_sse_server_with_url() -> None:
    entry = McpServerEntry(id="test", url="http://localhost:9222/sse", transport="sse")
    assert entry.transport == "sse"
    assert entry.url == "http://localhost:9222/sse"


def test_stdio_server_requires_command() -> None:
    with pytest.raises(ValidationError, match="command is required"):
        McpServerEntry(id="test", transport="stdio")


def test_stdio_server_with_command() -> None:
    entry = McpServerEntry(
        id="test",
        transport="stdio",
        command="npx",
        args=["-y", "@anthropic/mcp-server"],
    )
    assert entry.transport == "stdio"
    assert entry.command == "npx"
    assert entry.args == ["-y", "@anthropic/mcp-server"]


def test_streamable_http_server_requires_url() -> None:
    with pytest.raises(ValidationError, match="url is required"):
        McpServerEntry(id="test", transport="streamable-http")


def test_streamable_http_server_with_url() -> None:
    entry = McpServerEntry(id="test", url="http://localhost:8080/mcp", transport="streamable-http")
    assert entry.transport == "streamable-http"


def test_default_transport_is_sse() -> None:
    entry = McpServerEntry(id="test", url="http://localhost:9222/sse")
    assert entry.transport == "sse"
