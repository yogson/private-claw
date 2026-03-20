"""Tests for merging capability claude_code_settings into Claude settings.json."""

import json
from pathlib import Path

from assistant.core.capabilities.loader import apply_claude_code_settings
from assistant.core.capabilities.schemas import (
    CapabilityDefinition,
    ClaudeCodePermissions,
    ClaudeCodeSettings,
)


def test_apply_claude_code_settings_writes_mcp_servers(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    definitions = {
        "delegation_coding": CapabilityDefinition(
            capability_id="delegation_coding",
            prompt="",
            tools=[],
            claude_code_settings=ClaudeCodeSettings(
                permissions=ClaudeCodePermissions(
                    allow=["Read(*)"],
                    deny=[],
                ),
                mcp_servers={
                    "logfire": {
                        "type": "http",
                        "url": "https://logfire-us.pydantic.dev/mcp",
                    }
                },
            ),
        ),
    }
    apply_claude_code_settings(
        definitions,
        ["delegation_coding"],
        settings_path=settings_path,
    )
    data = json.loads(settings_path.read_text())
    assert data["permissions"]["allow"] == ["Read(*)"]
    assert data["mcpServers"]["logfire"]["type"] == "http"
    assert data["mcpServers"]["logfire"]["url"] == "https://logfire-us.pydantic.dev/mcp"


def test_apply_claude_code_settings_mcp_only_preserves_permissions(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"permissions": {"allow": ["Bash(git:*)"], "deny": []}}) + "\n"
    )
    definitions = {
        "cap_a": CapabilityDefinition(
            capability_id="cap_a",
            prompt="",
            tools=[],
            claude_code_settings=ClaudeCodeSettings(
                permissions=ClaudeCodePermissions(),
                mcp_servers={"logfire": {"type": "http", "url": "https://example/mcp"}},
            ),
        ),
    }
    apply_claude_code_settings(definitions, ["cap_a"], settings_path=settings_path)
    data = json.loads(settings_path.read_text())
    assert data["permissions"]["allow"] == ["Bash(git:*)"]
    assert "logfire" in data["mcpServers"]


def test_apply_claude_code_settings_leaves_mcp_when_none_defined(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "permissions": {"allow": [], "deny": []},
                "mcpServers": {"other": {"type": "stdio", "command": "npx"}},
            }
        )
        + "\n"
    )
    definitions = {
        "x": CapabilityDefinition(
            capability_id="x",
            prompt="",
            tools=[],
            claude_code_settings=ClaudeCodeSettings(
                permissions=ClaudeCodePermissions(allow=["Read(*)"], deny=[]),
            ),
        ),
    }
    apply_claude_code_settings(definitions, ["x"], settings_path=settings_path)
    data = json.loads(settings_path.read_text())
    assert data["mcpServers"]["other"]["command"] == "npx"
