"""Capability mcp_servers collection and bootstrap validation."""

from pathlib import Path
from unittest.mock import patch

import pytest

from assistant.core.capabilities.loader import collect_mcp_server_ids
from assistant.core.capabilities.schemas import CapabilityDefinition
from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesPolicyConfig,
    McpServerEntry,
    McpServersConfig,
    MemoryConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
    ToolsConfig,
)


def _config(tmp_path: Path, servers: list[McpServerEntry]) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root=str(tmp_path), timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="m", model_allowlist=["m"]),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=["a"]),
        tools=ToolsConfig(tools=[]),
        mcp_servers=McpServersConfig(servers=servers),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
        config_dir=tmp_path / "config",
    )


def test_collect_mcp_server_ids_follows_nested_and_dedupes() -> None:
    from assistant.core.capabilities.loader import expand_nested_capabilities

    defs = {
        "a": CapabilityDefinition(
            capability_id="a", mcp_servers=["finance"], nested_capabilities=["b"]
        ),
        "b": CapabilityDefinition(capability_id="b", mcp_servers=["opgraph", "finance"]),
        "c": CapabilityDefinition(capability_id="c", mcp_servers=["other"]),
    }
    enabled = expand_nested_capabilities(["a"], defs)
    assert collect_mcp_server_ids(defs, enabled) == ["finance", "opgraph"]


def test_unknown_server_warns_without_crashing(tmp_path: Path) -> None:
    from assistant.core.bootstrap import _validate_mcp_capabilities

    defs = {"a": CapabilityDefinition(capability_id="a", mcp_servers=["ghost"])}
    with patch("assistant.core.bootstrap.logger") as mock_logger:
        _validate_mcp_capabilities(_config(tmp_path, []), defs, ["a"])
    mock_logger.warning.assert_called_once()
    assert mock_logger.warning.call_args[0][0] == "bootstrap.mcp.server_not_configured"


def test_disabled_server_warns_without_crashing(tmp_path: Path) -> None:
    from assistant.core.bootstrap import _validate_mcp_capabilities

    defs = {"a": CapabilityDefinition(capability_id="a", mcp_servers=["off"])}
    servers = [McpServerEntry(id="off", url="http://h/sse", enabled=False)]
    with patch("assistant.core.bootstrap.logger") as mock_logger:
        _validate_mcp_capabilities(_config(tmp_path, servers), defs, ["a"])
    assert mock_logger.warning.call_args[0][0] == "bootstrap.mcp.server_disabled"


def test_known_enabled_server_is_silent(tmp_path: Path) -> None:
    from assistant.core.bootstrap import _validate_mcp_capabilities

    defs = {"a": CapabilityDefinition(capability_id="a", mcp_servers=["ok"])}
    servers = [McpServerEntry(id="ok", url="http://h/sse")]
    with patch("assistant.core.bootstrap.logger") as mock_logger:
        _validate_mcp_capabilities(_config(tmp_path, servers), defs, ["a"])
    mock_logger.warning.assert_not_called()


def test_no_first_party_tool_uses_the_mcp_prefix() -> None:
    """mcp_ must unambiguously mean 'MCP tool' (orchestrator mismatch check relies on it)."""
    from importlib import import_module

    import yaml

    from assistant.extensions.mcp.toolsets import MCP_TOOL_PREFIX

    root = Path(__file__).resolve().parents[4] / "config"
    tools = yaml.safe_load((root / "tools.yaml").read_text())["tools"]
    for tool in tools:
        assert not tool["tool_id"].startswith(MCP_TOOL_PREFIX)
        module_path, attr = tool["entrypoint"].split(":")
        func = getattr(import_module(module_path), attr)
        assert not getattr(func, "__name__", attr).startswith(MCP_TOOL_PREFIX)


def test_tracked_capabilities_reference_servers_not_tools() -> None:
    import yaml

    root = Path(__file__).resolve().parents[4] / "config" / "capabilities"
    finance = yaml.safe_load((root / "finance_mcp.yaml").read_text())
    assert finance["mcp_servers"] == ["finance"]
    assert "tools" not in finance
    for path in root.glob("*.yaml"):
        assert "cap.mcp." not in path.read_text(), path.name


def test_bootstrap_rejects_first_party_tool_with_mcp_prefix(tmp_path: Path) -> None:
    from assistant.core.bootstrap import _validate_tools_and_capabilities
    from assistant.core.capabilities.schemas import CapabilityToolBinding

    config = _config(tmp_path, [])
    config.capabilities.enabled_capabilities = ["a"]
    defs = {
        "a": CapabilityDefinition(
            capability_id="a", tools=[CapabilityToolBinding(tool_id="mcp_sneaky")]
        )
    }
    with pytest.raises(SystemExit, match="reserved for MCP"):
        _validate_tools_and_capabilities(config, defs)
