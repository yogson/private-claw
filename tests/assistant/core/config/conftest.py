"""Shared fixtures for config tests."""

from pathlib import Path

import pytest
import yaml

VALID_APP = {"runtime_mode": "prod", "data_root": "./data", "timezone": "UTC"}
VALID_TELEGRAM = {"enabled": False, "bot_token": "", "allowlist": []}
VALID_MODEL = {"default_model_id": "claude-sonnet", "model_allowlist": ["claude-sonnet"]}
VALID_CAPABILITIES = {"enabled_capabilities": ["assistant"], "denied_capabilities": []}
VALID_TOOLS = {
    "tools": [
        {
            "tool_id": "memory_search",
            "entrypoint": "assistant.agent.tools.memory_search:memory_search",
        },
        {
            "tool_id": "memory_propose_update",
            "entrypoint": "assistant.agent.tools.memory_propose_update:memory_propose_update",
        },
        {
            "tool_id": "ask_question",
            "entrypoint": "assistant.agent.tools.ask_question:ask_question",
        },
        {
            "tool_id": "shell_execute_readonly",
            "entrypoint": "assistant.agent.tools.shell_execute:shell_execute_readonly",
        },
    ]
}
VALID_MCP = {"servers": []}
VALID_SCHEDULER = {"tick_seconds": 10, "retry_policy": {"max_attempts": 3, "backoff_seconds": 60}}
VALID_STORE = {"backend": "filesystem", "lock_ttl_seconds": 30, "atomic_write": True}
VALID_MEMORY = {"api_key": "test", "default_user_id": "default"}


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Create a temporary config directory with all valid YAML files."""
    files = {
        "app.yaml": VALID_APP,
        "channel.telegram.yaml": VALID_TELEGRAM,
        "model.yaml": VALID_MODEL,
        "capabilities.yaml": VALID_CAPABILITIES,
        "tools.yaml": VALID_TOOLS,
        "mcp_servers.yaml": VALID_MCP,
        "scheduler.yaml": VALID_SCHEDULER,
        "store.yaml": VALID_STORE,
        "memory.yaml": VALID_MEMORY,
    }
    for filename, data in files.items():
        (tmp_path / filename).write_text(yaml.dump(data))
    return tmp_path
