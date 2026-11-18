"""Capability resolution is shared (denied / nested) and driven by one snapshot, not disk."""

from unittest.mock import patch

import pytest

from assistant.agent.pydantic_ai_agent import _create_agent
from assistant.agent.system_prompt_builder import _compose_system_prompt
from assistant.agent.tools.registry import (
    active_capabilities_for,
    build_tool_runtime_params,
    collect_enabled_mcp_server_ids,
    collect_enabled_tool_ids,
)
from assistant.core.capabilities.claude_code_settings import collect_claude_code_mcp_servers
from assistant.core.capabilities.resolver import resolve_policy
from assistant.core.capabilities.schemas import (
    CapabilityDefinition,
    CapabilityToolBinding,
    ClaudeCodeSettings,
)
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
from tests.assistant.capability_helpers import active_for, snapshot_of


def _config(enabled: list[str], denied: list[str], tools: list[str] | None = None) -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=enabled, denied_capabilities=denied
        ),
        tools=ToolsConfig(
            tools=[ToolDefinition(tool_id=t, entrypoint=f"mod:{t}") for t in tools or []]
        ),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


def _cap(cap_id: str, tool_id: str, nested: list[str] | None = None) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=cap_id,
        prompt=f"PROMPT-{cap_id}",
        tools=[CapabilityToolBinding(tool_id=tool_id, enabled=True)],
        nested_capabilities=nested or [],
        mcp_servers=[f"srv_{cap_id}"],
        claude_code_settings=ClaudeCodeSettings(mcp_servers={f"cc_{cap_id}": {"command": "x"}}),
    )


def test_collect_enabled_tool_ids_skips_denied() -> None:
    config = _config(["a", "b"], ["b"])
    defs = {"a": _cap("a", "tool_a"), "b": _cap("b", "tool_b")}
    assert collect_enabled_tool_ids(active_for(config, defs)) == {"tool_a"}


def test_agent_build_never_reads_manifests_from_disk() -> None:
    config = _config(["a"], [])
    snapshot = snapshot_of({"a": _cap("a", "tool_a")})
    with patch(
        "assistant.core.capabilities.loader.load_capability_definitions",
        side_effect=AssertionError("no disk reads on the agent build path"),
    ):
        _create_agent("test", "prompt", config, active_capabilities_for(config, snapshot))


# Bug 2: a denied parent's nested children used to stay active, because nested expansion
# ran before the denied filter. Every call site now uses the single resolver.
_DENIED_PARENT_DEFS = {
    "base": _cap("base", "tool_base", nested=["shared"]),
    "parent": _cap("parent", "tool_parent", nested=["child", "shared"]),
    "child": _cap("child", "tool_child", nested=["grandchild"]),
    "grandchild": _cap("grandchild", "tool_grandchild"),
    "shared": _cap("shared", "tool_shared"),
}
_EXPECTED_ACTIVE = {"base", "shared"}


@pytest.fixture()
def denied_parent_config() -> RuntimeConfig:
    return _config(
        ["base", "parent"],
        ["parent"],
        tools=[f"tool_{c}" for c in _DENIED_PARENT_DEFS],
    )


def test_denied_parent_children_excluded_from_resolver(
    denied_parent_config: RuntimeConfig,
) -> None:
    snapshot = snapshot_of(_DENIED_PARENT_DEFS)
    assert resolve_policy(denied_parent_config.capabilities, snapshot) == ["base", "shared"]


def test_denied_parent_children_excluded_from_tools(denied_parent_config: RuntimeConfig) -> None:
    active = active_for(denied_parent_config, _DENIED_PARENT_DEFS)
    assert collect_enabled_tool_ids(active) == {f"tool_{c}" for c in _EXPECTED_ACTIVE}


def test_denied_parent_children_excluded_from_tool_params(
    denied_parent_config: RuntimeConfig,
) -> None:
    params = build_tool_runtime_params(denied_parent_config, snapshot_of(_DENIED_PARENT_DEFS))
    assert set(params) == {f"tool_{c}" for c in _EXPECTED_ACTIVE}


def test_denied_parent_children_excluded_from_prompt(denied_parent_config: RuntimeConfig) -> None:
    prompt = _compose_system_prompt(active_for(denied_parent_config, _DENIED_PARENT_DEFS))
    assert "PROMPT-base" in prompt
    assert "PROMPT-shared" in prompt
    for cap_id in ("parent", "child", "grandchild"):
        assert f"PROMPT-{cap_id}" not in prompt


def test_denied_parent_children_excluded_from_mcp_servers(
    denied_parent_config: RuntimeConfig,
) -> None:
    active = active_for(denied_parent_config, _DENIED_PARENT_DEFS)
    assert set(collect_enabled_mcp_server_ids(active)) == {f"srv_{c}" for c in _EXPECTED_ACTIVE}
    subagent = collect_claude_code_mcp_servers(*active)
    assert set(subagent) == {f"cc_{c}" for c in _EXPECTED_ACTIVE}


def test_child_enabled_explicitly_survives_denied_parent() -> None:
    config = _config(["parent", "child"], ["parent"])
    snapshot = snapshot_of(_DENIED_PARENT_DEFS)
    assert resolve_policy(config.capabilities, snapshot) == ["child", "grandchild"]
