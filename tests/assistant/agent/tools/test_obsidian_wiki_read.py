"""Tests for obsidian_wiki_read tool."""

from pathlib import Path

import pytest

from assistant.agent.tools.obsidian_wiki_read import obsidian_wiki_read


class _Ctx:
    pass


def test_returns_unavailable_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_OBSIDIAN_VAULT", raising=False)
    result = obsidian_wiki_read(_Ctx(), path="wiki/index.md")
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("path", ["/etc/passwd", "wiki/../secrets.md", "../outside.md", ""])
def test_rejects_unsafe_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", str(tmp_path))
    result = obsidian_wiki_read(_Ctx(), path=path)
    assert result["status"] == "rejected_invalid"


def test_returns_not_found_for_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", str(tmp_path))
    result = obsidian_wiki_read(_Ctx(), path="wiki/missing.md")
    assert result["status"] == "not_found"


def test_reads_exact_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", str(tmp_path))
    (tmp_path / "wiki").mkdir()
    content = "# Index\n\nsome content\n"
    (tmp_path / "wiki" / "index.md").write_text(content, encoding="utf-8")
    result = obsidian_wiki_read(_Ctx(), path="wiki/index.md")
    assert result["status"] == "ok"
    assert result["path"] == "wiki/index.md"
    assert result["content"] == content


def test_rejects_oversized_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", str(tmp_path))
    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_read._MAX_READ_BYTES", 10)
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "big.md").write_text("x" * 100, encoding="utf-8")
    result = obsidian_wiki_read(_Ctx(), path="wiki/big.md")
    assert result["status"] == "rejected_invalid"
    assert "too large" in result["reason"]
