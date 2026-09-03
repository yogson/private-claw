"""Tests for shared obsidian_wiki path-safety helper."""

import pytest

from assistant.agent.tools.obsidian_wiki_common import reject_unsafe_vault_path


@pytest.mark.parametrize(
    "path",
    ["", "  ", "/etc/passwd", "wiki/../secrets.md", "../outside.md", ".."],
)
def test_rejects_unsafe_paths(path: str) -> None:
    assert reject_unsafe_vault_path(path) is not None


@pytest.mark.parametrize("path", ["wiki/index.md", "wiki/projects/private-claw/overview.md"])
def test_accepts_safe_paths(path: str) -> None:
    assert reject_unsafe_vault_path(path) is None
