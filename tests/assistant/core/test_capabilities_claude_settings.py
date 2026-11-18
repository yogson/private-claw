"""Tests for merging capability claude_code_settings into Claude settings.json."""

import json
from pathlib import Path

from assistant.core.capabilities.claude_code_settings import (
    ClaudeCodeSettingsSync,
    SubagentMcpServersSync,
    apply_claude_code_settings,
    collect_claude_code_mcp_servers,
)
from assistant.core.capabilities.schemas import (
    CapabilityDefinition,
    ClaudeCodePermissions,
    ClaudeCodeSettings,
)
from assistant.core.capabilities.snapshot import CapabilitySnapshot
from tests.assistant.capability_helpers import snapshot_of


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


def test_apply_claude_code_settings_denied_cap_excluded_when_caller_filters(
    tmp_path: Path,
) -> None:
    """Denied capabilities' settings must not appear when caller passes a filtered list.

    apply_claude_code_settings does not expand or filter denied caps itself — callers
    are responsible for passing an already-denied-filtered list (consistent contract).
    """
    settings_path = tmp_path / "settings.json"
    definitions = {
        "parent": CapabilityDefinition(
            capability_id="parent",
            prompt="",
            tools=[],
            nested_capabilities=["child"],
            claude_code_settings=ClaudeCodeSettings(
                permissions=ClaudeCodePermissions(allow=["Read(*)"]),
            ),
        ),
        "child": CapabilityDefinition(
            capability_id="child",
            prompt="",
            tools=[],
            claude_code_settings=ClaudeCodeSettings(
                permissions=ClaudeCodePermissions(allow=["Bash(rm:*)"]),
                mcp_servers={"secret": {"type": "http", "url": "https://secret/mcp"}},
            ),
        ),
    }
    # Caller pre-filters "child" (denied) before calling apply_claude_code_settings
    apply_claude_code_settings(definitions, ["parent"], settings_path=settings_path)
    data = json.loads(settings_path.read_text())
    assert "Bash(rm:*)" not in data["permissions"]["allow"]
    assert "secret" not in data.get("mcpServers", {})


def test_collect_claude_code_mcp_servers_merges_in_order() -> None:
    definitions = {
        "a": CapabilityDefinition(
            capability_id="a",
            claude_code_settings=ClaudeCodeSettings(
                mcp_servers={
                    "shared": {"type": "http", "url": "https://first"},
                    "ui-skills": {"type": "http", "url": "https://www.ui-skills.com/mcp"},
                }
            ),
        ),
        "b": CapabilityDefinition(
            capability_id="b",
            claude_code_settings=ClaudeCodeSettings(
                mcp_servers={"shared": {"type": "http", "url": "https://second"}}
            ),
        ),
        "c": CapabilityDefinition(capability_id="c"),
    }
    merged = collect_claude_code_mcp_servers(definitions, ["a", "b", "c", "missing"])
    assert merged == {
        "shared": {"type": "http", "url": "https://second"},
        "ui-skills": {"type": "http", "url": "https://www.ui-skills.com/mcp"},
    }
    # Deep-copied: callers may not mutate the capability definitions.
    merged["shared"]["url"] = "mutated"
    assert definitions["b"].claude_code_settings is not None
    assert definitions["b"].claude_code_settings.mcp_servers["shared"]["url"] == "https://second"


def test_settings_sync_rewrites_only_when_claude_code_settings_change(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"

    def _snap(revision: int, allow: list[str], prompt: str = "") -> CapabilitySnapshot:
        return snapshot_of(
            {
                "a": CapabilityDefinition(
                    capability_id="a",
                    prompt=prompt,
                    claude_code_settings=ClaudeCodeSettings(
                        permissions=ClaudeCodePermissions(allow=allow)
                    ),
                )
            },
            revision=revision,
        )

    sync = ClaudeCodeSettingsSync(
        _snap(1, ["Read"]),
        resolve_active=lambda snapshot: ["a"],
        settings_path=settings_path,
    )
    sync(_snap(2, ["Read"], prompt="prompt-only change"))
    assert not settings_path.exists()
    sync(_snap(3, ["Read", "Bash(git:*)"]))
    assert json.loads(settings_path.read_text())["permissions"]["allow"] == ["Read", "Bash(git:*)"]


def test_subagent_mcp_sync_pushes_servers_of_each_revision() -> None:
    received: list[dict[str, dict[str, object]]] = []

    class _Backend:
        def set_mcp_servers(self, mcp_servers: dict[str, dict[str, object]]) -> None:
            received.append(mcp_servers)

    def _snap(revision: int, servers: dict[str, dict[str, object]]) -> CapabilitySnapshot:
        definition = CapabilityDefinition(
            capability_id="a", claude_code_settings=ClaudeCodeSettings(mcp_servers=servers)
        )
        return snapshot_of({"a": definition}, revision=revision)

    sync = SubagentMcpServersSync(resolve_active=lambda snapshot: ["a"], targets=[_Backend()])
    sync(_snap(1, {"one": {"command": "x"}}))
    sync(_snap(2, {"two": {"command": "y"}}))
    assert received == [{"one": {"command": "x"}}, {"two": {"command": "y"}}]
