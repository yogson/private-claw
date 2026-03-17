"""Tests for MCP bridge: loader, models, bridge."""

from pathlib import Path

import pytest

from assistant.core.config.schemas import (
    McpDefaults,
    McpServerEntry,
    McpServersConfig,
    McpTimeouts,
    RuntimeConfig,
)
from assistant.extensions.mcp.bridge import McpBridge
from assistant.extensions.mcp.loader import capability_id_for_mcp_tool, load_tool_mappings
from assistant.extensions.mcp.models import RiskClass


def test_capability_id_for_mcp_tool() -> None:
    assert capability_id_for_mcp_tool("chrome_devtools", "browser_navigate") == (
        "cap.mcp.chrome_devtools.browser_navigate"
    )


def test_load_tool_mappings_empty_when_no_plugins_dir(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    plugins_mcp = config_dir.parent / "plugins" / "mcp"
    assert not plugins_mcp.exists()
    result = load_tool_mappings(config_dir)
    assert result == {}


def test_load_tool_mappings_loads_from_subdir(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    plugins_mcp = tmp_path / "plugins" / "mcp"
    plugins_mcp.mkdir(parents=True)
    server_dir = plugins_mcp / "chrome_devtools"
    server_dir.mkdir()
    tool_map = server_dir / "tool_map.yaml"
    tool_map.write_text(
        """
server_id: chrome_devtools
tools:
  - tool_name: browser_navigate
    summary: Navigate to URL
    risk_class: interactive
  - tool_name: browser_snapshot
    summary: Capture page
    risk_class: readonly
"""
    )
    result = load_tool_mappings(config_dir)
    assert "chrome_devtools" in result
    mapping = result["chrome_devtools"]
    assert mapping.server_id == "chrome_devtools"
    assert len(mapping.tools) == 2
    assert mapping.tools[0].tool_name == "browser_navigate"
    assert mapping.tools[0].risk_class == RiskClass.INTERACTIVE
    assert mapping.tools[1].tool_name == "browser_snapshot"
    assert mapping.tools[1].risk_class == RiskClass.READONLY


def test_load_tool_mappings_duplicate_server_id_raises(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    plugins_mcp = tmp_path / "plugins" / "mcp"
    plugins_mcp.mkdir(parents=True)
    for sub in ["a", "b"]:
        d = plugins_mcp / sub
        d.mkdir()
        (d / "tool_map.yaml").write_text("server_id: dup\ntools: []")
    with pytest.raises(ValueError, match="Duplicate MCP server_id"):
        load_tool_mappings(config_dir)


def test_mcp_bridge_get_tools_empty_when_no_mappings(
    tmp_path: Path, minimal_runtime_config: RuntimeConfig
) -> None:
    bridge = McpBridge(minimal_runtime_config)
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.unknown.browser_navigate"})
    assert tools == []


def test_mcp_bridge_get_tools_skips_when_server_not_enabled(
    tmp_path: Path, minimal_runtime_config: RuntimeConfig
) -> None:
    plugins_mcp = tmp_path / "plugins" / "mcp"
    plugins_mcp.mkdir(parents=True)
    (plugins_mcp / "chrome_devtools").mkdir()
    (plugins_mcp / "chrome_devtools" / "tool_map.yaml").write_text(
        "server_id: chrome_devtools\ntools:\n  - tool_name: browser_navigate\n    summary: Nav"
    )
    config = minimal_runtime_config.model_copy(update={"config_dir": tmp_path / "config"})
    config.config_dir = tmp_path / "config"
    config.mcp_servers = McpServersConfig(
        servers=[], defaults=McpDefaults(), timeouts=McpTimeouts()
    )
    bridge = McpBridge(config)
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.chrome_devtools.browser_navigate"})
    assert tools == []


def test_mcp_bridge_get_tools_returns_tools_when_server_enabled(
    tmp_path: Path, minimal_runtime_config: RuntimeConfig
) -> None:
    plugins_mcp = tmp_path / "plugins" / "mcp"
    plugins_mcp.mkdir(parents=True)
    (plugins_mcp / "chrome_devtools").mkdir()
    (plugins_mcp / "chrome_devtools" / "tool_map.yaml").write_text(
        "server_id: chrome_devtools\ntools:\n  - tool_name: browser_navigate\n    summary: Nav"
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    config = minimal_runtime_config.model_copy(update={"config_dir": config_dir})
    config.mcp_servers = McpServersConfig(
        servers=[
            McpServerEntry(
                id="chrome_devtools",
                url="http://localhost:9999/sse",
                enabled=True,
                tool_policy="deny_by_default",
            )
        ],
        defaults=McpDefaults(),
        timeouts=McpTimeouts(),
    )
    bridge = McpBridge(config)
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.chrome_devtools.browser_navigate"})
    assert len(tools) == 1
    assert tools[0].name == "cap.mcp.chrome_devtools.browser_navigate"
    assert "Nav" in (tools[0].description or "")


def test_mcp_bridge_skips_tool_when_server_policy_deny(
    tmp_path: Path, minimal_runtime_config: RuntimeConfig
) -> None:
    plugins_mcp = tmp_path / "plugins" / "mcp"
    plugins_mcp.mkdir(parents=True)
    (plugins_mcp / "chrome_devtools").mkdir()
    (plugins_mcp / "chrome_devtools" / "tool_map.yaml").write_text(
        "server_id: chrome_devtools\ntools:\n  - tool_name: browser_navigate\n    summary: Nav"
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    config = minimal_runtime_config.model_copy(update={"config_dir": config_dir})
    config.mcp_servers = McpServersConfig(
        servers=[
            McpServerEntry(
                id="chrome_devtools",
                url="http://localhost:9999/sse",
                enabled=True,
                tool_policy="deny",
            )
        ],
        defaults=McpDefaults(),
        timeouts=McpTimeouts(),
    )
    bridge = McpBridge(config)
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.chrome_devtools.browser_navigate"})
    assert tools == []


def test_mcp_bridge_skips_tool_not_in_allowlist(
    tmp_path: Path, minimal_runtime_config: RuntimeConfig
) -> None:
    plugins_mcp = tmp_path / "plugins" / "mcp"
    plugins_mcp.mkdir(parents=True)
    (plugins_mcp / "chrome_devtools").mkdir()
    (plugins_mcp / "chrome_devtools" / "tool_map.yaml").write_text(
        "server_id: chrome_devtools\ntools:\n  - tool_name: browser_navigate\n    summary: Nav"
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    config = minimal_runtime_config.model_copy(update={"config_dir": config_dir})
    config.mcp_servers = McpServersConfig(
        servers=[
            McpServerEntry(
                id="chrome_devtools",
                url="http://localhost:9999/sse",
                enabled=True,
                tool_policy="deny_by_default",
            )
        ],
        defaults=McpDefaults(),
        timeouts=McpTimeouts(),
    )
    bridge = McpBridge(config)
    tools = bridge.get_tools_for_capability_ids({"cap.mcp.chrome_devtools.browser_click"})
    assert tools == []


@pytest.fixture
def minimal_runtime_config(tmp_path: Path) -> RuntimeConfig:
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        ModelConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolsConfig,
    )

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return RuntimeConfig(
        app=AppConfig(data_root=str(tmp_path), timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(
            default_model_id="claude-3-5-haiku", model_allowlist=["claude-3-5-haiku"]
        ),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=["assistant"]),
        tools=ToolsConfig(tools=[]),
        mcp_servers=McpServersConfig(servers=[], defaults=McpDefaults(), timeouts=McpTimeouts()),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        config_dir=config_dir,
    )
