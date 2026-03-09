"""Shared fixtures for config tests."""

from pathlib import Path

import pytest
import yaml

VALID_APP = {"runtime_mode": "prod", "data_root": "./data", "timezone": "UTC"}
VALID_TELEGRAM = {"enabled": False, "bot_token": "", "allowlist": []}
VALID_MODEL = {"default_model_id": "claude-sonnet", "model_allowlist": ["claude-sonnet"]}
VALID_CAPABILITIES = {"allowed_capabilities": ["cap.memory.read"]}
VALID_MCP = {"servers": []}
VALID_SCHEDULER = {"tick_seconds": 10, "retry_policy": {"max_attempts": 3, "backoff_seconds": 60}}
VALID_STORE = {"backend": "filesystem", "lock_ttl_seconds": 30, "atomic_write": True}


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with all valid YAML files."""
    files = {
        "app.yaml": VALID_APP,
        "channel.telegram.yaml": VALID_TELEGRAM,
        "model.yaml": VALID_MODEL,
        "capabilities.yaml": VALID_CAPABILITIES,
        "mcp_servers.yaml": VALID_MCP,
        "scheduler.yaml": VALID_SCHEDULER,
        "store.yaml": VALID_STORE,
    }
    for filename, data in files.items():
        (tmp_path / filename).write_text(yaml.dump(data))
    return tmp_path
