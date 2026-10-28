"""Tests for obsidian_wiki_edit tool."""

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import assistant.agent.tools.obsidian_wiki_edit as edit_module
from assistant.agent.tools.obsidian_wiki_edit import obsidian_wiki_edit


class _Ctx:
    pass


def _set_env(monkeypatch: pytest.MonkeyPatch, vault: str, product: str = "/product") -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", vault)
    monkeypatch.setenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", product)


def test_returns_unavailable_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_OBSIDIAN_VAULT", raising=False)
    result = obsidian_wiki_edit(_Ctx(), path="wiki/note.md", old_string="a", new_string="b")
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("path", ["/etc/passwd", "wiki/../secrets.md", "../outside.md"])
def test_rejects_unsafe_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str) -> None:
    _set_env(monkeypatch, str(tmp_path))
    result = obsidian_wiki_edit(_Ctx(), path=path, old_string="a", new_string="b")
    assert result["status"] == "rejected_invalid"


def test_rejects_empty_old_string(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    result = obsidian_wiki_edit(_Ctx(), path="wiki/note.md", old_string="", new_string="b")
    assert result["status"] == "rejected_invalid"


def test_rejects_identical_strings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    result = obsidian_wiki_edit(_Ctx(), path="wiki/note.md", old_string="same", new_string="same")
    assert result["status"] == "rejected_invalid"


def test_rejects_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    result = obsidian_wiki_edit(_Ctx(), path="wiki/missing.md", old_string="a", new_string="b")
    assert result["status"] == "rejected_invalid"
    assert "no such file" in result["reason"]


def test_rejects_old_string_not_found(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "note.md").write_text("hello world")
    result = obsidian_wiki_edit(_Ctx(), path="wiki/note.md", old_string="nope", new_string="b")
    assert result["status"] == "rejected_invalid"
    assert "not found" in result["reason"]


def test_rejects_non_unique_old_string_without_replace_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env(monkeypatch, str(tmp_path))
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "note.md").write_text("foo foo foo")
    result = obsidian_wiki_edit(_Ctx(), path="wiki/note.md", old_string="foo", new_string="bar")
    assert result["status"] == "rejected_invalid"
    assert "not unique" in result["reason"]


def test_successful_edit_delegates_to_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env(monkeypatch, str(tmp_path), product="/product")
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "note.md").write_text("# Title\n\nold paragraph.\n", encoding="utf-8")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] in ("/product/scripts/contextual-prefix.py", "/product/scripts/bm25-index.py"):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        bundle_path = cmd[4]
        bundle = json.loads(Path(bundle_path).read_text())
        assert bundle["operation_type"] == "markdown"
        assert bundle["writes"][0]["mode"] == "replace"
        assert bundle["writes"][0]["content"] == "# Title\n\nnew paragraph.\n"
        if cmd[3] == "inspect":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"approval_sha256": "abc123"}), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout=json.dumps({"operation_id": "edit-1", "changed_paths": ["wiki/note.md"]}),
            stderr="",
        )

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_edit(
        _Ctx(), path="wiki/note.md", old_string="old paragraph.", new_string="new paragraph."
    )
    assert result["status"] == "ok"
    assert result["changed_paths"] == ["wiki/note.md"]


def test_replace_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path), product="/product")
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "note.md").write_text("foo foo foo", encoding="utf-8")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] in ("/product/scripts/contextual-prefix.py", "/product/scripts/bm25-index.py"):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        bundle_path = cmd[4]
        bundle = json.loads(Path(bundle_path).read_text())
        assert bundle["writes"][0]["content"] == "bar bar bar"
        if cmd[3] == "inspect":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"approval_sha256": "abc123"}), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout=json.dumps({"operation_id": "edit-1", "changed_paths": ["wiki/note.md"]}),
            stderr="",
        )

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_edit(
        _Ctx(),
        path="wiki/note.md",
        old_string="foo",
        new_string="bar",
        replace_all=True,
    )
    assert result["status"] == "ok"


def test_precondition_pins_hash_of_content_the_edit_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A concurrent change after the edit's read must not become the expected
    hash, or the stale edit would silently overwrite it."""
    _set_env(monkeypatch, str(tmp_path), product="/product")
    (tmp_path / "wiki").mkdir()
    note = tmp_path / "wiki" / "note.md"
    original = "# Title\n\nold paragraph.\n"
    note.write_text(original, encoding="utf-8")

    real_write = edit_module.write_vault_transaction

    def _write_after_concurrent_change(*args: Any, **kwargs: Any) -> dict[str, Any]:
        note.write_text("# Title\n\nsomeone else's change.\n", encoding="utf-8")
        return real_write(*args, **kwargs)

    seen: dict[str, Any] = {}

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] in ("/product/scripts/contextual-prefix.py", "/product/scripts/bm25-index.py"):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        seen["bundle"] = json.loads(Path(cmd[4]).read_text())
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=json.dumps({"approval_sha256": "abc123"}), stderr=""
        )

    monkeypatch.setattr(edit_module, "write_vault_transaction", _write_after_concurrent_change)
    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    obsidian_wiki_edit(
        _Ctx(), path="wiki/note.md", old_string="old paragraph.", new_string="new paragraph."
    )
    expected = hashlib.sha256(original.encode("utf-8")).hexdigest()
    assert seen["bundle"]["expected_hashes"] == {"wiki/note.md": expected}
