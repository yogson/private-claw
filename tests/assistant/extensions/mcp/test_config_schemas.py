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


def test_http_server_accepts_headers() -> None:
    entry = McpServerEntry(
        id="remote",
        transport="streamable-http",
        url="https://example.com/mcp",
        headers={"Authorization": "Bearer placeholder"},
    )
    assert entry.headers == {"Authorization": "Bearer placeholder"}


def test_stdio_server_rejects_headers_and_url() -> None:
    with pytest.raises(ValidationError, match="HTTP transports only"):
        McpServerEntry(id="x", transport="stdio", command="python3", headers={"A": "b"})
    with pytest.raises(ValidationError, match="HTTP transports only"):
        McpServerEntry(id="x", transport="stdio", command="python3", url="http://h")


def test_http_server_rejects_stdio_fields() -> None:
    with pytest.raises(ValidationError, match="stdio"):
        McpServerEntry(id="x", transport="sse", url="http://h/sse", command="python3")


def test_include_and_exclude_tools_are_mutually_exclusive() -> None:
    with pytest.raises(ValidationError, match="mutually exclusive"):
        McpServerEntry(id="x", url="http://h/sse", include_tools=["a"], exclude_tools=["b"])
    assert McpServerEntry(id="x", url="http://h/sse", include_tools=["a"]).include_tools == ["a"]


def test_unknown_transport_rejected() -> None:
    with pytest.raises(ValidationError):
        McpServerEntry(id="x", transport="websocket", url="http://h")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_id", ["", "has space", "dot.ted", "-lead"])
def test_server_id_must_be_tool_name_safe(bad_id: str) -> None:
    with pytest.raises(ValidationError):
        McpServerEntry(id=bad_id, url="http://h/sse")


def test_defaults_for_new_fields() -> None:
    entry = McpServerEntry(id="x", url="http://h/sse")
    assert entry.include_instructions is True
    assert entry.confirm_tools == []
    assert entry.include_tools is None and entry.exclude_tools is None


def test_tracked_config_loads_with_curated_finance_tools() -> None:
    """config/mcp_servers.yaml parses and keeps the curated finance subset."""
    from pathlib import Path

    import yaml

    from assistant.core.config.schemas import McpServersConfig

    root = Path(__file__).resolve().parents[4] / "config"
    config = McpServersConfig(**yaml.safe_load((root / "mcp_servers.yaml").read_text()))
    finance = next(s for s in config.servers if s.id == "finance")
    assert finance.transport == "stdio"
    assert finance.include_tools is not None and len(finance.include_tools) == 58
    assert "get_exchange_rate" in finance.include_tools
