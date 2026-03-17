"""Tests for the bootstrap entry point."""

from pathlib import Path

import pytest
import yaml

from assistant.core.bootstrap import bootstrap
from assistant.core.config.schemas import RuntimeConfig


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    files = {
        "app.yaml": {"runtime_mode": "prod", "data_root": "./data", "timezone": "UTC"},
        "channel.telegram.yaml": {"enabled": False, "bot_token": "", "allowlist": []},
        "model.yaml": {"default_model_id": "claude-sonnet", "model_allowlist": ["claude-sonnet"]},
        "capabilities.yaml": {"enabled_capabilities": ["assistant"], "denied_capabilities": []},
        "tools.yaml": {
            "tools": [
                {
                    "tool_id": "memory_search",
                    "entrypoint": "assistant.agent.tools.memory_search:memory_search",
                    "enabled": True,
                },
                {
                    "tool_id": "memory_propose_update",
                    "entrypoint": (
                        "assistant.agent.tools.memory_propose_update:memory_propose_update"
                    ),
                    "enabled": True,
                },
                {
                    "tool_id": "ask_question",
                    "entrypoint": "assistant.agent.tools.ask_question:ask_question",
                    "enabled": True,
                },
                {
                    "tool_id": "shell_execute_readonly",
                    "entrypoint": "assistant.agent.tools.shell_execute:shell_execute_readonly",
                    "enabled": True,
                },
            ]
        },
        "mcp_servers.yaml": {"servers": []},
        "scheduler.yaml": {
            "tick_seconds": 10,
            "retry_policy": {"max_attempts": 3, "backoff_seconds": 60},
        },
        "store.yaml": {"backend": "filesystem", "lock_ttl_seconds": 30, "atomic_write": True},
        "memory.yaml": {"api_key": "test", "default_user_id": "default"},
    }
    for filename, data in files.items():
        (tmp_path / filename).write_text(yaml.dump(data))
    (tmp_path / "capabilities").mkdir(exist_ok=True)
    (tmp_path / "capabilities" / "assistant.yaml").write_text(
        yaml.dump(
            {
                "capability_id": "assistant",
                "prompt": "",
                "tools": [
                    {"tool_id": "memory_search", "enabled": True},
                    {"tool_id": "memory_propose_update", "enabled": True},
                    {"tool_id": "ask_question", "enabled": True},
                    {"tool_id": "shell_execute_readonly", "enabled": True},
                ],
            }
        )
    )
    return tmp_path


def test_bootstrap_returns_runtime_config(config_dir: Path) -> None:
    cfg = bootstrap(config_dir)
    assert isinstance(cfg, RuntimeConfig)


def test_bootstrap_exits_on_invalid_config(config_dir: Path) -> None:
    (config_dir / "app.yaml").unlink()
    with pytest.raises(SystemExit, match="Startup failed"):
        bootstrap(config_dir)


def test_bootstrap_uses_env_config_dir(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASSISTANT_CONFIG_DIR", str(config_dir))
    cfg = bootstrap()
    assert isinstance(cfg, RuntimeConfig)


def test_bootstrap_fails_when_enabled_capability_missing_manifest(config_dir: Path) -> None:
    """Enabled capability without manifest in config/capabilities/*.yaml causes startup failure."""
    with open(config_dir / "capabilities.yaml") as f:
        data = yaml.safe_load(f)
    data["enabled_capabilities"].append("nonexistent_cap")
    (config_dir / "capabilities.yaml").write_text(yaml.dump(data))
    with pytest.raises(SystemExit, match="nonexistent_cap"):
        bootstrap(config_dir)


def test_bootstrap_fails_when_capability_references_missing_tool(config_dir: Path) -> None:
    """Capability binding to tool_id not in tools.yaml causes startup failure."""
    (config_dir / "capabilities" / "bad.yaml").write_text(
        "capability_id: bad\nprompt: ''\ntools:\n  - tool_id: nonexistent_tool\n    enabled: true"
    )
    with open(config_dir / "capabilities.yaml") as f:
        data = yaml.safe_load(f)
    data["enabled_capabilities"].append("bad")
    (config_dir / "capabilities.yaml").write_text(yaml.dump(data))
    with pytest.raises(SystemExit, match="nonexistent_tool"):
        bootstrap(config_dir)


def test_bootstrap_fails_when_capability_references_disabled_tool(config_dir: Path) -> None:
    """Capability binding to tool that is disabled in tools.yaml causes startup failure."""
    (config_dir / "capabilities" / "deploy.yaml").write_text(
        yaml.dump(
            {
                "capability_id": "deploy",
                "prompt": "",
                "tools": [{"tool_id": "shell_execute_allowlisted", "enabled": True}],
            }
        )
    )
    with open(config_dir / "capabilities.yaml") as f:
        data = yaml.safe_load(f)
    data["enabled_capabilities"].append("deploy")
    (config_dir / "capabilities.yaml").write_text(yaml.dump(data))
    with open(config_dir / "tools.yaml") as f:
        tools_data = yaml.safe_load(f)
    tools_data["tools"].append(
        {
            "tool_id": "shell_execute_allowlisted",
            "entrypoint": "assistant.agent.tools.shell_execute:shell_execute_allowlisted",
            "enabled": False,
        }
    )
    (config_dir / "tools.yaml").write_text(yaml.dump(tools_data))
    with pytest.raises(SystemExit, match="disabled in tools.yaml"):
        bootstrap(config_dir)


def test_bootstrap_fails_when_tool_entrypoint_invalid(config_dir: Path) -> None:
    """Tool with bad entrypoint causes startup failure."""
    with open(config_dir / "tools.yaml") as f:
        data = yaml.safe_load(f)
    data["tools"].append(
        {
            "tool_id": "broken",
            "entrypoint": "nonexistent.module:no_callable",
            "enabled": True,
        }
    )
    (config_dir / "tools.yaml").write_text(yaml.dump(data))
    (config_dir / "capabilities" / "assistant.yaml").write_text(
        yaml.dump(
            {
                "capability_id": "assistant",
                "prompt": "",
                "tools": [
                    {"tool_id": "memory_search", "enabled": True},
                    {"tool_id": "memory_propose_update", "enabled": True},
                    {"tool_id": "ask_question", "enabled": True},
                    {"tool_id": "shell_execute_readonly", "enabled": True},
                    {"tool_id": "broken", "enabled": True},
                ],
            }
        )
    )
    with pytest.raises(SystemExit, match="failed to resolve"):
        bootstrap(config_dir)


def test_bootstrap_succeeds_with_macos_personal_capability(config_dir: Path) -> None:
    """Bootstrap succeeds when macos_personal capability and tools are correctly configured."""
    (config_dir / "capabilities" / "macos_personal.yaml").write_text(
        yaml.dump(
            {
                "capability_id": "macos_personal",
                "prompt": "",
                "tools": [
                    {"tool_id": "macos_notes_read", "enabled": True},
                    {"tool_id": "macos_notes_write", "enabled": True},
                    {"tool_id": "macos_reminders_read", "enabled": True},
                    {"tool_id": "macos_reminders_write", "enabled": True},
                ],
            }
        )
    )
    with open(config_dir / "capabilities.yaml") as f:
        cap_data = yaml.safe_load(f)
    cap_data["enabled_capabilities"].append("macos_personal")
    (config_dir / "capabilities.yaml").write_text(yaml.dump(cap_data))
    with open(config_dir / "tools.yaml") as f:
        tools_data = yaml.safe_load(f)
    tools_data["tools"].extend(
        [
            {
                "tool_id": "macos_notes_read",
                "entrypoint": "assistant.agent.tools.macos_tools:macos_notes_read",
                "enabled": True,
            },
            {
                "tool_id": "macos_notes_write",
                "entrypoint": "assistant.agent.tools.macos_tools:macos_notes_write",
                "enabled": True,
            },
            {
                "tool_id": "macos_reminders_read",
                "entrypoint": "assistant.agent.tools.macos_tools:macos_reminders_read",
                "enabled": True,
            },
            {
                "tool_id": "macos_reminders_write",
                "entrypoint": "assistant.agent.tools.macos_tools:macos_reminders_write",
                "enabled": True,
            },
        ]
    )
    (config_dir / "tools.yaml").write_text(yaml.dump(tools_data))
    cfg = bootstrap(config_dir)
    assert isinstance(cfg, RuntimeConfig)
