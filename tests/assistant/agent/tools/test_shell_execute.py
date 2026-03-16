"""Tests for shell_execute_readonly and shell_execute_allowlisted tools."""

from unittest.mock import MagicMock, patch

from assistant.agent.tools.deps import TurnDeps
from assistant.agent.tools.shell_execute import shell_execute_allowlisted, shell_execute_readonly
from assistant.core.config.schemas import CommandAllowlistEntry

_READONLY_FOR_TESTS = [
    "cat",
    "date",
    "env",
    "file",
    "find",
    "grep",
    "head",
    "ls",
    "pwd",
    "stat",
    "tail",
    "wc",
    "which",
    "whoami",
]

_READONLY_PARAMS = {
    "shell_execute_readonly": {"shell_readonly_commands": _READONLY_FOR_TESTS},
}


def _ctx(deps: TurnDeps) -> MagicMock:
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


@patch("assistant.agent.tools.shell_execute.subprocess.run")
def test_shell_execute_readonly_pwd(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="/tmp", stderr="")
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params=_READONLY_PARAMS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "pwd")
    assert result["status"] == "ok"
    assert result["stdout"] == "/tmp"
    mock_run.assert_called_once()


@patch("assistant.agent.tools.shell_execute.subprocess.run")
def test_shell_execute_readonly_ls(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="file1\nfile2", stderr="")
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params=_READONLY_PARAMS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "ls -la")
    assert result["status"] == "ok"
    mock_run.assert_called_once_with(["ls", "-la"], capture_output=True, text=True, timeout=15)


def test_shell_execute_readonly_rejects_disallowed() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params=_READONLY_PARAMS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "rm -rf /")
    assert result["status"] == "rejected_not_allowed"
    assert "rm" in result["reason"]


def test_shell_execute_readonly_rejects_empty() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params=_READONLY_PARAMS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "")
    assert result["status"] == "rejected_invalid"


def test_shell_execute_readonly_empty_config_rejects_unavailable() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params={},
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "pwd")
    assert result["status"] == "rejected_unavailable"
    assert "shell_readonly_commands" in result["reason"]


def test_shell_execute_allowlisted_empty_allowlist() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params={"shell_execute_allowlisted": {"command_allowlist": []}},
    )
    ctx = _ctx(deps)
    result = shell_execute_allowlisted(ctx, "gh pr list")
    assert result["status"] == "rejected_unavailable"
    assert "command_allowlist" in result["reason"]


def test_shell_execute_allowlisted_not_in_allowlist() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params={
            "shell_execute_allowlisted": {
                "command_allowlist": [CommandAllowlistEntry(id="git", command_pattern="git")],
            },
        },
    )
    ctx = _ctx(deps)
    result = shell_execute_allowlisted(ctx, "gh pr list")
    assert result["status"] == "rejected_not_allowed"


@patch("assistant.agent.tools.shell_execute.subprocess.run")
def test_shell_execute_allowlisted_allowed(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="hello", stderr="")
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params={
            "shell_execute_allowlisted": {
                "command_allowlist": [CommandAllowlistEntry(id="echo", command_pattern="echo")],
            },
        },
    )
    ctx = _ctx(deps)
    result = shell_execute_allowlisted(ctx, "echo hello")
    assert result["status"] == "ok"
    assert "hello" in result.get("stdout", "")


def test_shell_execute_allowlisted_rejects_empty_command() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params={
            "shell_execute_allowlisted": {
                "command_allowlist": [CommandAllowlistEntry(id="ls", command_pattern="ls")],
            },
        },
    )
    ctx = _ctx(deps)
    result = shell_execute_allowlisted(ctx, "")
    assert result["status"] == "rejected_invalid"


@patch("assistant.agent.tools.shell_execute.subprocess.run")
def test_shell_execute_readonly_timeout(mock_run: MagicMock) -> None:
    import subprocess

    mock_run.side_effect = subprocess.TimeoutExpired("find", 15)
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params=_READONLY_PARAMS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "find / -name foo")
    assert result["status"] == "timeout"
    assert "timed out" in result["reason"].lower()


def test_shell_execute_readonly_malformed_quoting() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params=_READONLY_PARAMS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, 'ls "unclosed')
    assert result["status"] == "rejected_invalid"


def test_shell_execute_allowlisted_rejects_args_not_matching_pattern() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        tool_runtime_params={
            "shell_execute_allowlisted": {
                "command_allowlist": [
                    CommandAllowlistEntry(
                        id="gh",
                        command_pattern="gh",
                        allowed_args_pattern=r"pr list|pr view",
                    )
                ],
            },
        },
    )
    ctx = _ctx(deps)
    result = shell_execute_allowlisted(ctx, "gh repo clone foo/bar")
    assert result["status"] == "rejected_not_allowed"


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_get_agent_tools_included_when_global_and_capability_enabled(
    mock_load_caps: MagicMock,
) -> None:
    """Tool is registered when enabled in tools.yaml and in active capability."""
    from assistant.agent.tools.registry import get_agent_tools
    from assistant.core.capabilities.schemas import (
        CapabilityDefinition,
        CapabilityToolBinding,
    )
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        McpServersConfig,
        ModelConfig,
        RuntimeConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolDefinition,
        ToolsConfig,
    )

    mock_load_caps.return_value = {
        "assistant": CapabilityDefinition(
            capability_id="assistant",
            prompt="",
            tools=[
                CapabilityToolBinding(tool_id="shell_execute_readonly", enabled=True),
            ],
        ),
    }
    tools_config = ToolsConfig(
        tools=[
            ToolDefinition(
                tool_id="shell_execute_readonly",
                entrypoint="assistant.agent.tools.shell_execute:shell_execute_readonly",
                enabled=True,
            ),
        ]
    )
    config = RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["assistant"],
            denied_capabilities=[],
        ),
        tools=tools_config,
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
    )
    tools = get_agent_tools(config)
    tool_names = [getattr(t, "__name__", str(t)) for t in tools]
    assert "shell_execute_readonly" in tool_names


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_get_agent_tools_excluded_when_global_disabled(
    mock_load_caps: MagicMock,
) -> None:
    """Tool is not registered when disabled in tools.yaml even if capability enables it."""
    from assistant.agent.tools.registry import get_agent_tools
    from assistant.core.capabilities.schemas import (
        CapabilityDefinition,
        CapabilityToolBinding,
    )
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        McpServersConfig,
        ModelConfig,
        RuntimeConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolDefinition,
        ToolsConfig,
    )

    mock_load_caps.return_value = {
        "assistant": CapabilityDefinition(
            capability_id="assistant",
            prompt="",
            tools=[
                CapabilityToolBinding(tool_id="shell_execute_readonly", enabled=True),
            ],
        ),
    }
    tools_config = ToolsConfig(
        tools=[
            ToolDefinition(
                tool_id="shell_execute_readonly",
                entrypoint="assistant.agent.tools.shell_execute:shell_execute_readonly",
                enabled=False,
            ),
        ]
    )
    config = RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["assistant"],
            denied_capabilities=[],
        ),
        tools=tools_config,
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
    )
    tools = get_agent_tools(config)
    tool_names = [getattr(t, "__name__", str(t)) for t in tools]
    assert "shell_execute_readonly" not in tool_names


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_get_agent_tools_excluded_when_capability_disabled(
    mock_load_caps: MagicMock,
) -> None:
    """Tool is not registered when capability binding has enabled=false even if global enabled."""
    from assistant.agent.tools.registry import get_agent_tools
    from assistant.core.capabilities.schemas import (
        CapabilityDefinition,
        CapabilityToolBinding,
    )
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        McpServersConfig,
        ModelConfig,
        RuntimeConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolDefinition,
        ToolsConfig,
    )

    mock_load_caps.return_value = {
        "assistant": CapabilityDefinition(
            capability_id="assistant",
            prompt="",
            tools=[
                CapabilityToolBinding(tool_id="shell_execute_readonly", enabled=False),
            ],
        ),
    }
    tools_config = ToolsConfig(
        tools=[
            ToolDefinition(
                tool_id="shell_execute_readonly",
                entrypoint="assistant.agent.tools.shell_execute:shell_execute_readonly",
                enabled=True,
            ),
        ]
    )
    config = RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["assistant"],
            denied_capabilities=[],
        ),
        tools=tools_config,
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
    )
    tools = get_agent_tools(config)
    tool_names = [getattr(t, "__name__", str(t)) for t in tools]
    assert "shell_execute_readonly" not in tool_names


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_get_agent_tools_excludes_denied_capability(
    mock_load_caps: MagicMock,
) -> None:
    """When deploy capability is denied, shell_execute_allowlisted is not registered."""
    from assistant.agent.tools.registry import get_agent_tools
    from assistant.core.capabilities.schemas import (
        CapabilityDefinition,
        CapabilityToolBinding,
    )
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        McpServersConfig,
        ModelConfig,
        RuntimeConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolDefinition,
        ToolsConfig,
    )

    mock_load_caps.return_value = {
        "assistant": CapabilityDefinition(
            capability_id="assistant",
            prompt="",
            tools=[
                CapabilityToolBinding(tool_id="shell_execute_readonly", enabled=True),
            ],
        ),
        "deploy": CapabilityDefinition(
            capability_id="deploy",
            prompt="",
            tools=[
                CapabilityToolBinding(tool_id="shell_execute_allowlisted", enabled=True),
                CapabilityToolBinding(tool_id="shell_execute_readonly", enabled=True),
            ],
        ),
    }
    tools_config = ToolsConfig(
        tools=[
            ToolDefinition(
                tool_id="shell_execute_readonly",
                entrypoint="assistant.agent.tools.shell_execute:shell_execute_readonly",
            ),
            ToolDefinition(
                tool_id="shell_execute_allowlisted",
                entrypoint="assistant.agent.tools.shell_execute:shell_execute_allowlisted",
            ),
        ]
    )
    config = RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["assistant", "deploy"],
            denied_capabilities=["deploy"],
        ),
        tools=tools_config,
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
    )
    tools = get_agent_tools(config)
    tool_names = [getattr(t, "__name__", str(t)) for t in tools]
    assert "shell_execute_allowlisted" not in tool_names
    assert "shell_execute_readonly" in tool_names


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_build_tool_runtime_params_does_not_stamp_shell_fields_on_non_shell_tools(
    mock_load_caps: MagicMock,
) -> None:
    """Non-shell tools should not receive shell-specific default params implicitly."""
    from assistant.agent.tools.registry import build_tool_runtime_params
    from assistant.core.capabilities.schemas import CapabilityDefinition, CapabilityToolBinding
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        McpServersConfig,
        ModelConfig,
        RuntimeConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolDefinition,
        ToolsConfig,
    )

    mock_load_caps.return_value = {
        "assistant": CapabilityDefinition(
            capability_id="assistant",
            prompt="",
            tools=[CapabilityToolBinding(tool_id="memory_search", enabled=True)],
        )
    }
    config = RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["assistant"], denied_capabilities=[]
        ),
        tools=ToolsConfig(
            tools=[
                ToolDefinition(
                    tool_id="memory_search",
                    entrypoint="assistant.agent.tools.memory_search:memory_search",
                    enabled=True,
                )
            ]
        ),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
    )

    params = build_tool_runtime_params(config)
    assert params["memory_search"] == {}


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_build_tool_runtime_params_keeps_shell_defaults_for_shell_tools(
    mock_load_caps: MagicMock,
) -> None:
    """Shell tools keep only their explicitly configured params."""
    from assistant.agent.tools.registry import build_tool_runtime_params
    from assistant.core.capabilities.schemas import CapabilityDefinition, CapabilityToolBinding
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesPolicyConfig,
        McpServersConfig,
        ModelConfig,
        RuntimeConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
        ToolDefinition,
        ToolsConfig,
    )

    mock_load_caps.return_value = {
        "assistant": CapabilityDefinition(
            capability_id="assistant",
            prompt="",
            tools=[
                CapabilityToolBinding(tool_id="shell_execute_readonly", enabled=True),
                CapabilityToolBinding(tool_id="shell_execute_allowlisted", enabled=True),
            ],
        )
    }
    config = RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["assistant"], denied_capabilities=[]
        ),
        tools=ToolsConfig(
            tools=[
                ToolDefinition(
                    tool_id="shell_execute_readonly",
                    entrypoint="assistant.agent.tools.shell_execute:shell_execute_readonly",
                    enabled=True,
                    default_params={"shell_readonly_commands": ["ls", "pwd"]},
                ),
                ToolDefinition(
                    tool_id="shell_execute_allowlisted",
                    entrypoint="assistant.agent.tools.shell_execute:shell_execute_allowlisted",
                    enabled=True,
                    default_params={
                        "command_allowlist": [{"id": "echo", "command_pattern": "echo"}]
                    },
                ),
            ]
        ),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
    )

    params = build_tool_runtime_params(config)
    assert params["shell_execute_readonly"] == {"shell_readonly_commands": ["ls", "pwd"]}
    assert params["shell_execute_allowlisted"] == {
        "command_allowlist": [
            {
                "id": "echo",
                "command_pattern": "echo",
                "allowed_args_pattern": ".*",
                "max_timeout_seconds": 30,
            }
        ]
    }
