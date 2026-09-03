"""Tests for obsidian_wiki_write tool."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from assistant.agent.tools.obsidian_wiki_write import obsidian_wiki_write


class _Ctx:
    pass


def _set_env(monkeypatch: pytest.MonkeyPatch, vault: str, product: str = "/product") -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", vault)
    monkeypatch.setenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", product)


def test_returns_unavailable_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_OBSIDIAN_VAULT", raising=False)
    monkeypatch.delenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", raising=False)
    result = obsidian_wiki_write(_Ctx(), operation_type="save", writes=[])
    assert result["status"] == "unavailable"


def test_rejects_empty_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    result = obsidian_wiki_write(_Ctx(), operation_type="save", writes=[])
    assert result["status"] == "rejected_invalid"


def test_rejects_too_many_writes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    writes = [{"path": f"wiki/n{i}.md", "mode": "create", "content": "x"} for i in range(21)]
    result = obsidian_wiki_write(_Ctx(), operation_type="save", writes=writes)
    assert result["status"] == "rejected_invalid"
    assert "too many writes" in result["reason"]


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "wiki/../secrets.md", "../outside.md", ""],
)
def test_rejects_unsafe_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str) -> None:
    _set_env(monkeypatch, str(tmp_path))
    result = obsidian_wiki_write(
        _Ctx(), operation_type="save", writes=[{"path": path, "mode": "create", "content": "x"}]
    )
    assert result["status"] == "rejected_invalid"


def test_rejects_bad_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/note.md", "mode": "delete", "content": "x"}],
    )
    assert result["status"] == "rejected_invalid"
    assert "mode must be" in result["reason"]


def test_rejects_empty_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    writes = [{"path": "wiki/note.md", "mode": "create", "content": ""}]
    result = obsidian_wiki_write(_Ctx(), operation_type="save", writes=writes)
    assert result["status"] == "rejected_invalid"


def test_replace_missing_file_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/missing.md", "mode": "replace", "content": "x"}],
    )
    assert result["status"] == "rejected_invalid"
    assert "cannot replace missing file" in result["reason"]


def test_create_existing_file_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path))
    (tmp_path / "wiki").mkdir()
    (tmp_path / "wiki" / "note.md").write_text("existing")
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/note.md", "mode": "create", "content": "x"}],
    )
    assert result["status"] == "rejected_invalid"
    assert "already exists" in result["reason"]


def test_successful_create_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path), product="/product")
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        assert cmd[0] == "python3"
        if cmd[1] == "/product/scripts/contextual-prefix.py":
            assert cmd[2:] == ["--vault", str(tmp_path), "wiki/note.md", "--no-llm"]
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if cmd[1] == "/product/scripts/bm25-index.py":
            assert cmd[2:] == ["--vault", str(tmp_path), "build"]
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

        assert cmd[1] == "/product/scripts/claude-obsidian.py"
        assert cmd[2] == "transaction"
        bundle_path = cmd[4]
        bundle = json.loads(Path(bundle_path).read_text())
        assert bundle["schema"] == "claude-obsidian.transaction.v1"
        assert bundle["operation_type"] == "save"
        assert bundle["expected_hashes"] == {"wiki/note.md": None}
        assert bundle["writes"][0]["path"] == "wiki/note.md"
        assert bundle["writes"][0]["mode"] == "create"

        if cmd[3] == "inspect":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"approval_sha256": "abc123"}), stderr=""
            )
        assert cmd[3] == "apply"
        assert "--approved-plan-sha256" in cmd
        assert cmd[cmd.index("--approved-plan-sha256") + 1] == "abc123"
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout=json.dumps(
                {"operation_id": "save-xyz", "changed_paths": ["wiki/note.md"]}
            ),
            stderr="",
        )

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/note.md", "mode": "create", "content": "hello"}],
    )
    assert result["status"] == "ok"
    assert result["operation_id"] == "save-xyz"
    assert result["changed_paths"] == ["wiki/note.md"]
    assert result["index_refreshed"] is True
    assert len(calls) == 4

    # Temp bundle file must be cleaned up after the call.
    bundle_path = Path(calls[0][4])
    assert not bundle_path.exists()


def test_index_refresh_skipped_for_non_markdown_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-wiki/non-.md changed paths (e.g. index/ledger json) don't trigger reindexing."""
    _set_env(monkeypatch, str(tmp_path), product="/product")
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[3] == "inspect":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"approval_sha256": "abc123"}), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout=json.dumps(
                {"operation_id": "cfg-1", "changed_paths": [".vault-meta/mode.json"]}
            ),
            stderr="",
        )

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="configuration",
        writes=[{"path": ".vault-meta/mode.json", "mode": "create", "content": "{}"}],
    )
    assert result["status"] == "ok"
    assert result["index_refreshed"] is False
    assert len(calls) == 2  # only inspect + apply, no reindex calls


def test_index_refresh_failure_does_not_fail_the_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_env(monkeypatch, str(tmp_path), product="/product")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] == "/product/scripts/contextual-prefix.py":
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="boom")
        if cmd[3] == "inspect":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"approval_sha256": "abc123"}), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout=json.dumps({"operation_id": "save-xyz", "changed_paths": ["wiki/note.md"]}),
            stderr="",
        )

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/note.md", "mode": "create", "content": "hello"}],
    )
    assert result["status"] == "ok"
    assert result["index_refreshed"] is False
    assert result["index_refresh_error"] == "boom"


def test_replace_flow_computes_current_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import hashlib

    _set_env(monkeypatch, str(tmp_path), product="/product")
    (tmp_path / "wiki").mkdir()
    existing_content = "old content"
    (tmp_path / "wiki" / "note.md").write_text(existing_content, encoding="utf-8")
    expected_hash = hashlib.sha256(existing_content.encode("utf-8")).hexdigest()

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[1] in ("/product/scripts/contextual-prefix.py", "/product/scripts/bm25-index.py"):
            return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        bundle_path = cmd[4]
        bundle = json.loads(Path(bundle_path).read_text())
        assert bundle["expected_hashes"] == {"wiki/note.md": expected_hash}
        if cmd[3] == "inspect":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"approval_sha256": "abc123"}), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout=json.dumps({"operation_id": "save-xyz", "changed_paths": ["wiki/note.md"]}),
            stderr="",
        )

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/note.md", "mode": "replace", "content": "new content"}],
    )
    assert result["status"] == "ok"
    assert result["index_refreshed"] is True


def test_inspect_failure_returns_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path), product="/product")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[3] == "inspect"
        return subprocess.CompletedProcess(cmd, returncode=2, stdout="", stderr="ERR bad bundle")

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/note.md", "mode": "create", "content": "hello"}],
    )
    assert result["status"] == "failed"
    assert result["reason"] == "ERR bad bundle"


def test_apply_conflict_returns_conflict(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path), product="/product")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[3] == "inspect":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"approval_sha256": "abc123"}), stderr=""
            )
        return subprocess.CompletedProcess(cmd, returncode=75, stdout="", stderr="locked")

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/note.md", "mode": "create", "content": "hello"}],
    )
    assert result["status"] == "conflict"


def test_apply_generic_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path), product="/product")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if cmd[3] == "inspect":
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout=json.dumps({"approval_sha256": "abc123"}), stderr=""
            )
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/note.md", "mode": "create", "content": "hello"}],
    )
    assert result["status"] == "failed"
    assert result["reason"] == "boom"


def test_timeout_returns_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _set_env(monkeypatch, str(tmp_path), product="/product")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout=30)

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_write.subprocess.run", _fake_run)
    result = obsidian_wiki_write(
        _Ctx(),
        operation_type="save",
        writes=[{"path": "wiki/note.md", "mode": "create", "content": "hello"}],
    )
    assert result["status"] == "failed"
