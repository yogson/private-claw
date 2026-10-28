"""Tests for obsidian_wiki_list tool."""

from pathlib import Path

import pytest

from assistant.agent.tools.obsidian_wiki_list import obsidian_wiki_list


class _Ctx:
    pass


def test_returns_unavailable_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_OBSIDIAN_VAULT", raising=False)
    result = obsidian_wiki_list(_Ctx(), path="wiki")
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("path", ["/etc", "wiki/../secrets", "../outside"])
def test_rejects_unsafe_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", str(tmp_path))
    result = obsidian_wiki_list(_Ctx(), path=path)
    assert result["status"] == "rejected_invalid"


def test_returns_not_found_for_missing_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", str(tmp_path))
    result = obsidian_wiki_list(_Ctx(), path="wiki/missing")
    assert result["status"] == "not_found"


def test_lists_files_and_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", str(tmp_path))
    (tmp_path / "wiki" / "projects" / "demo").mkdir(parents=True)
    (tmp_path / "wiki" / "projects" / "demo" / "overview.md").write_text("x")
    (tmp_path / "wiki" / "projects" / "demo" / "sub").mkdir()

    result = obsidian_wiki_list(_Ctx(), path="wiki/projects/demo")
    assert result["status"] == "ok"
    assert result["path"] == "wiki/projects/demo"
    assert result["entries"] == [
        {"name": "overview.md", "type": "file"},
        {"name": "sub", "type": "dir"},
    ]


def test_lists_vault_root_when_path_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", str(tmp_path))
    (tmp_path / "wiki").mkdir()
    (tmp_path / "inbox").mkdir()

    result = obsidian_wiki_list(_Ctx())
    assert result["status"] == "ok"
    assert result["path"] == "."
    names = {e["name"] for e in result["entries"]}
    assert {"wiki", "inbox"} <= names
