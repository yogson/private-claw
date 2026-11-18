"""Tests for merging capability claude_code_settings into Claude settings.json."""

import errno
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

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


@pytest.fixture(autouse=True)
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never let a test reach the real ~/.claude/settings.json."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _defs(allow: list[str], mcp: dict[str, dict[str, object]] | None = None) -> dict:
    settings = ClaudeCodeSettings(
        permissions=ClaudeCodePermissions(allow=allow), mcp_servers=mcp or {}
    )
    return {"a": CapabilityDefinition(capability_id="a", claude_code_settings=settings)}


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


# ----------------------------------------------------------------------
# settings.json safety: never destroy, only touch owned keys, atomic
# ----------------------------------------------------------------------


def test_default_path_is_under_home(tmp_home: Path) -> None:
    assert apply_claude_code_settings(_defs(["Read"]), ["a"])
    data = json.loads((tmp_home / ".claude" / "settings.json").read_text())
    assert data["permissions"]["allow"] == ["Read"]


@pytest.mark.parametrize("content", ['{"hooks": {"Stop": []}, "model": "op', "[1, 2]", ""])
def test_unusable_existing_file_is_left_untouched(tmp_path: Path, content: str) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(content)
    with capture_logs() as logs:
        assert not apply_claude_code_settings(_defs(["Read"]), ["a"], settings_path)
    assert settings_path.read_text() == content
    assert [e["event"] for e in logs] == ["capabilities.claude_code_settings.skipped"]


def test_non_object_permissions_is_left_untouched(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"permissions": "ask"}\n')
    assert not apply_claude_code_settings(_defs(["Read"]), ["a"], settings_path)
    assert settings_path.read_text() == '{"permissions": "ask"}\n'


def test_unrelated_keys_are_preserved(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    existing = {
        "hooks": {"Stop": [{"command": "say done"}]},
        "env": {"FOO": "1"},
        "model": "opus",
        "statusLine": {"type": "command", "command": "x"},
        "permissions": {"allow": ["Old"], "defaultMode": "plan", "additionalDirectories": ["/w"]},
    }
    settings_path.write_text(json.dumps(existing))
    assert apply_claude_code_settings(
        _defs(["Read"], {"s": {"type": "http", "url": "u"}}), ["a"], settings_path
    )
    data = json.loads(settings_path.read_text())
    expected = {
        **existing,
        "permissions": {**existing["permissions"], "allow": ["Read"], "deny": []},
        "mcpServers": {"s": {"type": "http", "url": "u"}},
    }
    assert data == expected


def test_write_is_atomic_and_keeps_mode(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"model": "opus"}')
    settings_path.chmod(0o640)
    inode = settings_path.stat().st_ino
    assert apply_claude_code_settings(_defs(["Read"]), ["a"], settings_path)
    st = settings_path.stat()
    assert st.st_ino != inode  # replaced by rename, never truncated in place
    assert stat.S_IMODE(st.st_mode) == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home", "settings.json"]


def test_failed_write_leaves_original_intact(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text('{"model": "opus"}')
    with (
        patch("os.replace", side_effect=OSError(errno.ENOSPC, "No space left on device")),
        capture_logs() as logs,
    ):
        assert not apply_claude_code_settings(_defs(["Read"]), ["a"], settings_path)
    assert settings_path.read_text() == '{"model": "opus"}'
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home", "settings.json"]
    assert [e["event"] for e in logs] == ["capabilities.claude_code_settings.skipped"]
    assert "No space left on device" in logs[0]["error"]


@pytest.mark.parametrize(
    "target",
    ["tempfile.mkstemp", "os.fsync", "os.replace", "pathlib.Path.mkdir"],
)
def test_write_failure_is_logged_and_skipped(tmp_home: Path, target: str) -> None:
    error = PermissionError(errno.EACCES, "Permission denied")
    with patch(target, side_effect=error), capture_logs() as logs:
        assert not apply_claude_code_settings(_defs(["Read"]), ["a"])  # never raises
    assert [e["event"] for e in logs] == ["capabilities.claude_code_settings.skipped"]
    assert "Permission denied" in logs[0]["error"]
    claude_dir = tmp_home / ".claude"
    leftovers = sorted(p.name for p in claude_dir.iterdir()) if claude_dir.exists() else []
    assert leftovers == []  # no partial file, no temp file


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_claude_dir_is_skipped(tmp_home: Path) -> None:
    claude_dir = tmp_home / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text('{"model": "opus"}')
    claude_dir.chmod(0o500)
    try:
        with capture_logs() as logs:
            assert not apply_claude_code_settings(_defs(["Read"]), ["a"])
    finally:
        claude_dir.chmod(0o700)
    assert (claude_dir / "settings.json").read_text() == '{"model": "opus"}'
    assert [e["event"] for e in logs] == ["capabilities.claude_code_settings.skipped"]


def test_settings_sync_survives_and_retries_a_failed_write(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    sync = ClaudeCodeSettingsSync(
        snapshot_of(_defs(["Read"]), revision=1),
        resolve_active=lambda snapshot: ["a"],
        settings_path=settings_path,
    )
    with patch("os.replace", side_effect=OSError(errno.ENOSPC, "No space left on device")):
        sync(snapshot_of(_defs(["Read", "Bash"]), revision=2))  # never raises
    assert not settings_path.exists()
    sync(snapshot_of(_defs(["Read", "Bash"]), revision=3))  # same settings: retried
    assert json.loads(settings_path.read_text())["permissions"]["allow"] == ["Read", "Bash"]


def test_symlinked_settings_file_stays_a_link(tmp_path: Path) -> None:
    target = tmp_path / "dotfiles" / "settings.json"
    target.parent.mkdir()
    target.write_text('{"model": "opus"}')
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    assert apply_claude_code_settings(_defs(["Read"]), ["a"], link)
    assert link.is_symlink()
    assert json.loads(target.read_text())["permissions"]["allow"] == ["Read"]


def test_unchanged_settings_are_not_rewritten(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    assert apply_claude_code_settings(_defs(["Read"]), ["a"], settings_path)
    os.utime(settings_path, ns=(1_000_000_000, 1_000_000_000))
    assert apply_claude_code_settings(_defs(["Read"]), ["a"], settings_path)
    assert settings_path.stat().st_mtime_ns == 1_000_000_000


def test_settings_sync_retries_after_a_skipped_apply(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("{broken")
    sync = ClaudeCodeSettingsSync(
        snapshot_of(_defs(["Read"]), revision=1),
        resolve_active=lambda snapshot: ["a"],
        settings_path=settings_path,
    )
    sync(snapshot_of(_defs(["Read", "Bash"]), revision=2))
    assert settings_path.read_text() == "{broken"
    settings_path.write_text("{}")  # owner fixes the file
    sync(snapshot_of(_defs(["Read", "Bash"]), revision=3))
    assert json.loads(settings_path.read_text())["permissions"]["allow"] == ["Read", "Bash"]
