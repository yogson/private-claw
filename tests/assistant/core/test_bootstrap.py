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
        "capabilities.yaml": {"allowed_capabilities": ["cap.memory.read"]},
        "mcp_servers.yaml": {"servers": []},
        "scheduler.yaml": {
            "tick_seconds": 10,
            "retry_policy": {"max_attempts": 3, "backoff_seconds": 60},
        },
        "store.yaml": {"backend": "filesystem", "lock_ttl_seconds": 30, "atomic_write": True},
    }
    for filename, data in files.items():
        (tmp_path / filename).write_text(yaml.dump(data))
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
