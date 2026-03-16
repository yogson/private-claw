"""Tests for macOS Notes and Reminders tools."""

import subprocess
from unittest.mock import MagicMock, patch

from assistant.agent.tools.deps import TurnDeps
from assistant.agent.tools.macos_tools import (
    macos_notes_read,
    macos_notes_write,
    macos_reminders_read,
    macos_reminders_write,
)


def _ctx(deps: TurnDeps) -> MagicMock:
    ctx = MagicMock()
    ctx.deps = deps
    return ctx


def _deps() -> TurnDeps:
    return TurnDeps(writes_approved=[], seen_intent_ids=set(), tool_runtime_params={})


@patch("assistant.agent.tools.macos_tools.sys.platform", "linux")
def test_macos_notes_read_rejects_non_darwin() -> None:
    result = macos_notes_read(_ctx(_deps()))
    assert result["status"] == "rejected_platform"
    assert "macOS" in result["reason"]


@patch("assistant.agent.tools.macos_tools.sys.platform", "linux")
def test_macos_notes_write_rejects_non_darwin() -> None:
    result = macos_notes_write(_ctx(_deps()), title="Test", body="")
    assert result["status"] == "rejected_platform"


@patch("assistant.agent.tools.macos_tools.sys.platform", "linux")
def test_macos_reminders_read_rejects_non_darwin() -> None:
    result = macos_reminders_read(_ctx(_deps()))
    assert result["status"] == "rejected_platform"


@patch("assistant.agent.tools.macos_tools.sys.platform", "linux")
def test_macos_reminders_write_rejects_non_darwin() -> None:
    result = macos_reminders_write(_ctx(_deps()), title="Test")
    assert result["status"] == "rejected_platform"


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_notes_read_ok(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="Note1\x1fBody1\x1eNote2\x1fBody2\x1e",
        stderr="",
    )
    result = macos_notes_read(_ctx(_deps()), limit=10)
    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["data"][0] == {"name": "Note1", "body": "Body1"}
    assert result["data"][1] == {"name": "Note2", "body": "Body2"}
    mock_run.assert_called_once()
    call_args = mock_run.call_args
    assert "osascript" in call_args[0][0]


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_notes_read_with_folder_passes_folder_to_script(mock_run: MagicMock) -> None:
    """folder_name is passed to osascript when provided; script filters by folder."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="FolderNote\x1fFolderBody\x1e",
        stderr="",
    )
    result = macos_notes_read(_ctx(_deps()), folder_name="Work", limit=10)
    assert result["status"] == "ok"
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    assert cmd[0] == "osascript"
    assert "--" in cmd
    args = cmd[cmd.index("--") + 1 :]
    assert args == ["10", "Work"]


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_notes_read_without_folder_passes_empty(mock_run: MagicMock) -> None:
    """When folder_name is omitted, empty string is passed for all-notes scope."""
    mock_run.return_value = MagicMock(returncode=0, stdout="N\x1fB\x1e", stderr="")
    macos_notes_read(_ctx(_deps()), limit=5)
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    args = cmd[cmd.index("--") + 1 :]
    assert args == ["5", ""]


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_notes_write_ok(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="created", stderr="")
    result = macos_notes_write(_ctx(_deps()), title="My Note", body="Content")
    assert result["status"] == "ok"
    assert result["data"]["created"] is True
    assert result["data"]["title"] == "My Note"
    mock_run.assert_called_once()


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
def test_macos_notes_write_rejects_empty_title() -> None:
    result = macos_notes_write(_ctx(_deps()), title="", body="x")
    assert result["status"] == "rejected_invalid"
    assert "title" in result["reason"].lower()


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_reminders_read_ok(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="Rem1\x1fBody1\x1f2024-01-15\x1e",
        stderr="",
    )
    result = macos_reminders_read(_ctx(_deps()), limit=20)
    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["data"][0] == {
        "name": "Rem1",
        "body": "Body1",
        "due_date": "2024-01-15",
    }
    mock_run.assert_called_once()


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_reminders_read_without_list_name_passes_empty(mock_run: MagicMock) -> None:
    """When list_name is omitted, empty string is passed so script iterates all lists."""
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="R1\x1fB1\x1f\x1eR2\x1fB2\x1f2024-02-01\x1e",
        stderr="",
    )
    result = macos_reminders_read(_ctx(_deps()), list_name=None, limit=20)
    assert result["status"] == "ok"
    assert result["count"] == 2
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    args = cmd[cmd.index("--") + 1 :]
    assert args == ["20", ""]


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_reminders_read_timeout(mock_run: MagicMock) -> None:
    mock_run.side_effect = subprocess.TimeoutExpired("osascript", 20)
    result = macos_reminders_read(_ctx(_deps()))
    assert result["status"] == "timeout"
    assert "timed out" in result.get("reason", "").lower()


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_reminders_read_osascript_failure(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="Reminders got an error",
    )
    result = macos_reminders_read(_ctx(_deps()))
    assert result["status"] == "failed"
    assert "reason" in result


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_reminders_write_ok(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(returncode=0, stdout="created", stderr="")
    result = macos_reminders_write(
        _ctx(_deps()),
        title="Pay rent",
        body="Monthly",
        list_name="Tasks",
    )
    assert result["status"] == "ok"
    assert result["data"]["created"] is True
    mock_run.assert_called_once()


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
def test_macos_reminders_write_rejects_empty_title() -> None:
    result = macos_reminders_write(_ctx(_deps()), title="  ", body="x")
    assert result["status"] == "rejected_invalid"
    assert "title" in result["reason"].lower()


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_reminders_write_with_due_date_passes_components(mock_run: MagicMock) -> None:
    """When due components are provided, they are passed to osascript."""
    mock_run.return_value = MagicMock(returncode=0, stdout="created", stderr="")
    result = macos_reminders_write(
        _ctx(_deps()),
        title="Get some money",
        due_year=2026,
        due_month=3,
        due_day=17,
        due_hour=11,
        due_minute=0,
    )
    assert result["status"] == "ok"
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    args = cmd[cmd.index("--") + 1 :]
    assert args == ["Get some money", "", "", "2026", "3", "17", "11", "0"]


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_notes_read_timeout(mock_run: MagicMock) -> None:
    mock_run.side_effect = subprocess.TimeoutExpired("osascript", 20)
    result = macos_notes_read(_ctx(_deps()))
    assert result["status"] == "timeout"
    assert "timed out" in result.get("reason", "").lower()


@patch("assistant.agent.tools.macos_tools.sys.platform", "darwin")
@patch("assistant.agent.tools.macos_tools.subprocess.run")
def test_macos_notes_read_osascript_failure(mock_run: MagicMock) -> None:
    mock_run.return_value = MagicMock(
        returncode=1,
        stdout="",
        stderr="Notes got an error",
    )
    result = macos_notes_read(_ctx(_deps()))
    assert result["status"] == "failed"
    assert "reason" in result


@patch("assistant.agent.tools.registry.load_capability_definitions")
def test_get_agent_tools_includes_macos_when_capability_enabled(
    mock_load_caps: MagicMock,
) -> None:
    """macOS tools are registered when macos_personal capability is enabled."""
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
        "macos_personal": CapabilityDefinition(
            capability_id="macos_personal",
            prompt="",
            tools=[
                CapabilityToolBinding(tool_id="macos_notes_read", enabled=True),
                CapabilityToolBinding(tool_id="macos_notes_write", enabled=True),
                CapabilityToolBinding(tool_id="macos_reminders_read", enabled=True),
                CapabilityToolBinding(tool_id="macos_reminders_write", enabled=True),
            ],
        ),
    }
    tools_config = ToolsConfig(
        tools=[
            ToolDefinition(
                tool_id="macos_notes_read",
                entrypoint="assistant.agent.tools.macos_tools:macos_notes_read",
                enabled=True,
            ),
            ToolDefinition(
                tool_id="macos_notes_write",
                entrypoint="assistant.agent.tools.macos_tools:macos_notes_write",
                enabled=True,
            ),
            ToolDefinition(
                tool_id="macos_reminders_read",
                entrypoint="assistant.agent.tools.macos_tools:macos_reminders_read",
                enabled=True,
            ),
            ToolDefinition(
                tool_id="macos_reminders_write",
                entrypoint="assistant.agent.tools.macos_tools:macos_reminders_write",
                enabled=True,
            ),
        ]
    )
    config = RuntimeConfig(
        app=AppConfig(data_root="/tmp", timezone="UTC"),
        telegram=TelegramChannelConfig(),
        model=ModelConfig(default_model_id="x", model_allowlist=["x"]),
        capabilities=CapabilitiesPolicyConfig(
            enabled_capabilities=["macos_personal"],
            denied_capabilities=[],
        ),
        tools=tools_config,
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
    )
    tools = get_agent_tools(config)
    tool_names = [getattr(t, "__name__", str(t)) for t in tools]
    assert "macos_notes_read" in tool_names
    assert "macos_notes_write" in tool_names
    assert "macos_reminders_read" in tool_names
    assert "macos_reminders_write" in tool_names
