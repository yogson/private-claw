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
        shell_command_allowlist=[],
        shell_readonly_commands=_READONLY_FOR_TESTS,
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
        shell_command_allowlist=[],
        shell_readonly_commands=_READONLY_FOR_TESTS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "ls -la")
    assert result["status"] == "ok"
    mock_run.assert_called_once_with(["ls", "-la"], capture_output=True, text=True, timeout=15)


def test_shell_execute_readonly_rejects_disallowed() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        shell_command_allowlist=[],
        shell_readonly_commands=_READONLY_FOR_TESTS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "rm -rf /")
    assert result["status"] == "rejected_not_allowed"
    assert "rm" in result["reason"]


def test_shell_execute_readonly_rejects_empty() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        shell_command_allowlist=[],
        shell_readonly_commands=_READONLY_FOR_TESTS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "")
    assert result["status"] == "rejected_invalid"


def test_shell_execute_readonly_empty_config_rejects_unavailable() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        shell_command_allowlist=[],
        shell_readonly_commands=[],
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "pwd")
    assert result["status"] == "rejected_unavailable"
    assert "shell_readonly_commands" in result["reason"]


def test_shell_execute_allowlisted_empty_allowlist() -> None:
    deps = TurnDeps(writes_approved=[], seen_intent_ids=set(), shell_command_allowlist=[])
    ctx = _ctx(deps)
    result = shell_execute_allowlisted(ctx, "gh pr list")
    assert result["status"] == "rejected_unavailable"
    assert "command_allowlist" in result["reason"]


def test_shell_execute_allowlisted_not_in_allowlist() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        shell_command_allowlist=[CommandAllowlistEntry(id="git", command_pattern="git")],
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
        shell_command_allowlist=[CommandAllowlistEntry(id="echo", command_pattern="echo")],
    )
    ctx = _ctx(deps)
    result = shell_execute_allowlisted(ctx, "echo hello")
    assert result["status"] == "ok"
    assert "hello" in result.get("stdout", "")


def test_shell_execute_allowlisted_rejects_empty_command() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        shell_command_allowlist=[CommandAllowlistEntry(id="ls", command_pattern="ls")],
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
        shell_command_allowlist=[],
        shell_readonly_commands=_READONLY_FOR_TESTS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, "find / -name foo")
    assert result["status"] == "timeout"
    assert "timed out" in result["reason"].lower()


def test_shell_execute_readonly_malformed_quoting() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        shell_command_allowlist=[],
        shell_readonly_commands=_READONLY_FOR_TESTS,
    )
    ctx = _ctx(deps)
    result = shell_execute_readonly(ctx, 'ls "unclosed')
    assert result["status"] == "rejected_invalid"


def test_shell_execute_allowlisted_rejects_args_not_matching_pattern() -> None:
    deps = TurnDeps(
        writes_approved=[],
        seen_intent_ids=set(),
        shell_command_allowlist=[
            CommandAllowlistEntry(
                id="gh",
                command_pattern="gh",
                allowed_args_pattern=r"pr list|pr view",
            )
        ],
    )
    ctx = _ctx(deps)
    result = shell_execute_allowlisted(ctx, "gh repo clone foo/bar")
    assert result["status"] == "rejected_not_allowed"


def test_get_agent_tools_excludes_denied_capability() -> None:
    """When cap.shell.execute.allowlisted is denied, tool is not registered."""
    from assistant.agent.tools.registry import get_agent_tools
    from assistant.core.config.schemas import (
        AppConfig,
        CapabilitiesConfig,
        McpServersConfig,
        ModelConfig,
        RuntimeConfig,
        SchedulerConfig,
        StoreConfig,
        TelegramChannelConfig,
    )

    config = RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesConfig(
            allowed_capabilities=["cap.assistant.ask", "cap.shell.execute.readonly"],
            denied_capabilities=["cap.shell.execute.allowlisted"],
        ),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
    )
    tools = get_agent_tools(config)
    tool_names = [getattr(t, "__name__", str(t)) for t in tools]
    assert "shell_execute_allowlisted" not in tool_names
    assert "shell_execute_readonly" in tool_names
