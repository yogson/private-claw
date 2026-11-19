"""
Admin config apply/diff must never persist env-only override values to the YAML files.

Regression for #70: several instances share one config dir and differ only by env
overrides (ASSISTANT_APP_DATA_ROOT, the bot token, ...). Applying an unrelated key on
one instance must not write that instance's env values into the shared file.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import HTTPException

from assistant.api import deps
from assistant.api.routers.config import (
    ApplyRequest,
    ValidateRequest,
    apply_config,
    diff_config,
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
    ToolsConfig,
)

_FILE_APP = {"runtime_mode": "prod", "data_root": "./data", "timezone": "UTC"}
_FILE_TELEGRAM = {"enabled": False, "allowlist": [1], "poll_timeout_seconds": 30}


def _runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        app=AppConfig(data_root="data/instances/research", timezone="UTC"),
        telegram=TelegramChannelConfig(bot_token="env-only-token"),
        model=ModelConfig(default_model_id="m", model_allowlist=["m"]),
        capabilities=CapabilitiesPolicyConfig(enabled_capabilities=[], denied_capabilities=[]),
        tools=ToolsConfig(),
        mcp_servers=McpServersConfig(),
        scheduler=SchedulerConfig(),
        store=StoreConfig(),
        memory=MemoryConfig(api_key="test"),
    )


@pytest.fixture()
def shared_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "app.yaml").write_text(yaml.safe_dump(_FILE_APP))
    (tmp_path / "channel.telegram.yaml").write_text(yaml.safe_dump(_FILE_TELEGRAM))
    monkeypatch.setenv("ASSISTANT_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ASSISTANT_APP_DATA_ROOT", "data/instances/research")
    monkeypatch.setenv("ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN", "env-only-token")
    monkeypatch.setenv("ASSISTANT_LOGFIRE_TOKEN", "env-logfire-token")
    monkeypatch.setattr(deps, "_runtime_config", _runtime_config())
    return tmp_path


def _read(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


async def test_apply_app_key_does_not_persist_env_data_root(shared_config_dir: Path) -> None:
    resp = await apply_config(
        deps.get_runtime_config(),
        ApplyRequest(domain="app", payload={"log_level": "DEBUG"}, confirm=True),
    )

    assert resp.applied is True
    written = _read(shared_config_dir / "app.yaml")
    assert written == {**_FILE_APP, "log_level": "DEBUG"}
    assert "logfire_token" not in written
    # The live config still carries the env override after the reload.
    live = deps.get_runtime_config().app
    assert live.data_root == "data/instances/research"
    assert live.log_level == "DEBUG"


async def test_apply_telegram_key_does_not_persist_env_token(shared_config_dir: Path) -> None:
    resp = await apply_config(
        deps.get_runtime_config(),
        ApplyRequest(domain="telegram", payload={"poll_timeout_seconds": 45}, confirm=True),
    )

    assert resp.applied is True
    written_text = (shared_config_dir / "channel.telegram.yaml").read_text()
    assert "env-only-token" not in written_text
    assert yaml.safe_load(written_text) == {**_FILE_TELEGRAM, "poll_timeout_seconds": 45}
    assert deps.get_runtime_config().telegram.bot_token == "env-only-token"


async def test_apply_validates_with_env_overrides(
    shared_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """enabled=true needs a token; the env token satisfies validation but is not written."""
    resp = await apply_config(
        deps.get_runtime_config(),
        ApplyRequest(domain="telegram", payload={"allowlist": [1, 2]}, confirm=True),
    )
    assert resp.applied is True

    (shared_config_dir / "channel.telegram.yaml").write_text(
        yaml.safe_dump({**_FILE_TELEGRAM, "enabled": True})
    )
    resp = await apply_config(
        deps.get_runtime_config(),
        ApplyRequest(domain="telegram", payload={"poll_timeout_seconds": 10}, confirm=True),
    )
    assert resp.applied is True
    assert "bot_token" not in _read(shared_config_dir / "channel.telegram.yaml")

    monkeypatch.delenv("ASSISTANT_CHANNEL_TELEGRAM_BOT_TOKEN")
    with pytest.raises(HTTPException) as exc_info:
        await apply_config(
            deps.get_runtime_config(),
            ApplyRequest(domain="telegram", payload={"poll_timeout_seconds": 11}, confirm=True),
        )
    assert exc_info.value.status_code == 422


async def test_apply_nested_env_override_does_not_leak_into_file(
    shared_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler_file = {
        "tick_seconds": 10,
        "retry_policy": {"max_attempts": 3, "backoff_seconds": 60},
    }
    (shared_config_dir / "scheduler.yaml").write_text(yaml.safe_dump(scheduler_file))
    monkeypatch.setenv("ASSISTANT_SCHEDULER_RETRY_POLICY__MAX_ATTEMPTS", "9")

    resp = await apply_config(
        deps.get_runtime_config(),
        ApplyRequest(domain="scheduler", payload={"tick_seconds": 5}, confirm=True),
    )

    assert resp.applied is True
    assert _read(shared_config_dir / "scheduler.yaml") == {**scheduler_file, "tick_seconds": 5}


async def test_diff_compares_against_file_not_env(shared_config_dir: Path) -> None:
    resp = await diff_config(
        deps.get_runtime_config(),
        ValidateRequest(
            domain="telegram",
            payload={"bot_token": "", "poll_timeout_seconds": 30, "poll_interval_seconds": 1.5},
        ),
    )

    by_key = {c.key: c for c in resp.changes}
    # bot_token is not in the file: current is the schema default "", not the env token.
    assert "bot_token" not in by_key
    assert "poll_timeout_seconds" not in by_key
    assert by_key["poll_interval_seconds"].current == 0.0
    assert by_key["poll_interval_seconds"].proposed == 1.5
    assert all("env-only-token" not in str(c.current) for c in resp.changes)
