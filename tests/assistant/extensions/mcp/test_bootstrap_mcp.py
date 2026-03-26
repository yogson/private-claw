"""Tests for MCP-related bootstrap validation (P1 + P5)."""

from pathlib import Path
from unittest.mock import patch

import pytest

from assistant.core.config.schemas import (
    McpDefaults,
    McpServerEntry,
    McpServersConfig,
    McpTimeouts,
    RuntimeConfig,
)


def _make_runtime_config(
    tmp_path: Path, servers: list[McpServerEntry] | None = None
) -> RuntimeConfig:
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        MemoryConfig,
        ModelConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolsConfig,
    )

    return RuntimeConfig(
        app=AppConfig(data_root=str(tmp_path), timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(
            default_model_id="claude-3-5-haiku", model_allowlist=["claude-3-5-haiku"]
        ),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["assistant", "cap.mcp.chrome_devtools.browser_navigate"]
        ),
        tools=ToolsConfig(tools=[]),
        mcp_servers=McpServersConfig(
            servers=servers or [],
            defaults=McpDefaults(),
            timeouts=McpTimeouts(),
        ),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
        config_dir=tmp_path / "config",
    )


def test_bootstrap_mcp_exemption_does_not_crash(tmp_path: Path) -> None:
    """P1: cap.mcp.* IDs should not cause SystemExit in bootstrap validation loop."""
    from assistant.core.bootstrap import _validate_mcp_capabilities

    config = _make_runtime_config(tmp_path)
    all_enabled = ["assistant", "cap.mcp.chrome_devtools.browser_navigate"]
    # Should NOT raise — mcp caps are exempted from manifest check
    _validate_mcp_capabilities(config, all_enabled)


def test_bootstrap_mcp_cross_validation_warns_missing_server(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """P5: Warning when cap.mcp.* references a server not in mcp_servers.yaml."""
    from assistant.core.bootstrap import _validate_mcp_capabilities

    config = _make_runtime_config(tmp_path, servers=[])
    all_enabled = ["cap.mcp.chrome_devtools.browser_navigate"]
    with patch("assistant.core.bootstrap.logger") as mock_logger:
        _validate_mcp_capabilities(config, all_enabled)
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "bootstrap.mcp.server_not_configured"


def test_bootstrap_mcp_cross_validation_warns_disabled_server(tmp_path: Path) -> None:
    """P5: Warning when cap.mcp.* references a disabled server."""
    from assistant.core.bootstrap import _validate_mcp_capabilities

    config = _make_runtime_config(
        tmp_path,
        servers=[
            McpServerEntry(
                id="chrome_devtools",
                url="http://localhost:9222/sse",
                enabled=False,
            )
        ],
    )
    all_enabled = ["cap.mcp.chrome_devtools.browser_navigate"]
    with patch("assistant.core.bootstrap.logger") as mock_logger:
        _validate_mcp_capabilities(config, all_enabled)
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "bootstrap.mcp.server_disabled"


def test_bootstrap_mcp_cross_validation_no_warning_when_server_enabled(
    tmp_path: Path,
) -> None:
    """P5: No warning when server is properly configured and enabled."""
    from assistant.core.bootstrap import _validate_mcp_capabilities

    config = _make_runtime_config(
        tmp_path,
        servers=[
            McpServerEntry(
                id="chrome_devtools",
                url="http://localhost:9222/sse",
                enabled=True,
            )
        ],
    )
    all_enabled = ["cap.mcp.chrome_devtools.browser_navigate"]
    with patch("assistant.core.bootstrap.logger") as mock_logger:
        _validate_mcp_capabilities(config, all_enabled)
        mock_logger.warning.assert_not_called()
