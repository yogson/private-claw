"""Tests that tools.yaml max_retries reaches both plain and factory-built tools."""

from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai import Tool

from assistant.agent.tools.registry import get_agent_tools
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
    ToolDefinition,
    ToolsConfig,
)


def _config(definition: ToolDefinition) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["test_cap"], denied_capabilities=[]
        ),
        tools=ToolsConfig(tools=[definition]),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


def _capability(tool_id: str) -> dict[str, CapabilityDefinition]:
    return {
        "test_cap": CapabilityDefinition(
            capability_id="test_cap",
            prompt="",
            tools=[CapabilityToolBinding(tool_id=tool_id, enabled=True)],
        )
    }


@pytest.mark.parametrize("configured", [1, 3])
@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_factory_tool_gets_max_retries_from_tools_yaml(
    mock_load_caps: MagicMock,
    configured: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: factory tools used to silently ignore max_retries from tools.yaml."""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_load_caps.return_value = _capability("tavily_extract")

    config = _config(
        ToolDefinition(
            tool_id="tavily_extract",
            entrypoint="assistant.agent.tools.tavily_extract:get_tavily_extract_tool",
            enabled=True,
            max_retries=configured,
        )
    )
    tools = get_agent_tools(config)

    assert len(tools) == 1
    tool = tools[0]
    assert isinstance(tool, Tool)
    assert tool.max_retries == configured


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_factory_tool_without_max_retries_inherits_agent_default(
    mock_load_caps: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset max_retries stays None so the agent-level `retries` applies."""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    mock_load_caps.return_value = _capability("tavily_extract")

    config = _config(
        ToolDefinition(
            tool_id="tavily_extract",
            entrypoint="assistant.agent.tools.tavily_extract:get_tavily_extract_tool",
            enabled=True,
        )
    )
    tools = get_agent_tools(config)

    assert len(tools) == 1
    tool = tools[0]
    assert isinstance(tool, Tool)
    assert tool.max_retries is None
