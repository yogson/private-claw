"""Capability resolution is shared (denied / nested) and parsed once per agent build."""

from unittest.mock import MagicMock, patch

from assistant.agent.pydantic_ai_agent import _create_agent
from assistant.agent.tools.registry import collect_enabled_tool_ids
from assistant.core.capabilities.schemas import CapabilityDefinition, CapabilityToolBinding
from assistant.core.config.schemas import (
    AppConfig,
    CapabilitiesPolicyConfig,
    McpServersConfig,
    MemoryConfig,
    ModelConfig,
    RuntimeConfig,
    SchedulerConfig,
    StoreConfig,
    TelegramChannelConfig,
    ToolsConfig,
)


def _config(enabled: list[str], denied: list[str]) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=enabled, denied_capabilities=denied
        ),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


def _cap(cap_id: str, tool_id: str) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=cap_id,
        prompt="",
        tools=[CapabilityToolBinding(tool_id=tool_id, enabled=True)],
    )


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_collect_enabled_tool_ids_skips_denied(mock_load: MagicMock) -> None:
    mock_load.return_value = {"a": _cap("a", "tool_a"), "b": _cap("b", "tool_b")}
    assert collect_enabled_tool_ids(_config(["a", "b"], ["b"])) == {"tool_a"}


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_agent_build_parses_capability_manifests_once(mock_load: MagicMock) -> None:
    mock_load.return_value = {"a": _cap("a", "tool_a")}
    _create_agent("test", "prompt", _config(["a"], []))
    assert mock_load.call_count == 1
