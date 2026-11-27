"""Tests for the background obsidian retrieval-index refresher."""

import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import assistant.agent.tools.obsidian_wiki_index as index_module
from assistant.agent.tools.obsidian_wiki_index import index_is_stale, schedule_reindex


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(index_module, "_running", set())
    monkeypatch.setattr(index_module, "_dirty", set())
    monkeypatch.setattr(index_module, "_RETRY_DELAYS_SECONDS", (0.0, 0.0))


def _make_vault(tmp_path: Path) -> Path:
    (tmp_path / "wiki" / "sub").mkdir(parents=True)
    (tmp_path / "wiki" / "sub" / "a.md").write_text("a")
    (tmp_path / ".vault-meta" / "bm25").mkdir(parents=True)
    return tmp_path


def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def _wait_idle(timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while index_module._running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not index_module._running


def test_missing_index_is_stale(tmp_path: Path) -> None:
    _make_vault(tmp_path)
    assert index_is_stale(str(tmp_path)) is True


def test_index_newer_than_pages_is_fresh(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    index = vault / ".vault-meta" / "bm25" / "index.json"
    index.write_text("{}")
    for p in (vault / "wiki", vault / "wiki" / "sub", vault / "wiki" / "sub" / "a.md"):
        _set_mtime(p, 1000)
    _set_mtime(index, 2000)
    assert index_is_stale(str(vault)) is False


def test_page_newer_than_index_is_stale(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    index = vault / ".vault-meta" / "bm25" / "index.json"
    index.write_text("{}")
    for p in (vault / "wiki", vault / "wiki" / "sub"):
        _set_mtime(p, 1000)
    _set_mtime(index, 2000)
    _set_mtime(vault / "wiki" / "sub" / "a.md", 3000)
    assert index_is_stale(str(vault)) is True


def test_deleted_page_marks_index_stale_via_dir_mtime(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    index = vault / ".vault-meta" / "bm25" / "index.json"
    index.write_text("{}")
    _set_mtime(vault / "wiki", 1000)
    _set_mtime(vault / "wiki" / "sub" / "a.md", 1000)
    _set_mtime(index, 2000)
    _set_mtime(vault / "wiki" / "sub", 3000)  # e.g. a page was removed from sub/
    assert index_is_stale(str(vault)) is True


def test_reindex_runs_prefix_then_bm25_build(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(index_module.subprocess, "run", _fake_run)
    assert schedule_reindex("/product", "/vault", reason="test") is True
    _wait_idle()
    prefix = "/product/scripts/contextual-prefix.py"
    bm25 = "/product/scripts/bm25-index.py"
    assert calls == [
        ["python3", prefix, "--vault", "/vault", "--all", "--no-llm"],
        ["python3", bm25, "--vault", "/vault", "build"],
    ]


def test_requests_during_a_run_coalesce_into_one_follow_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    started = threading.Event()
    prefix_runs = 0

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal prefix_runs
        if cmd[1].endswith("contextual-prefix.py"):
            prefix_runs += 1
            if prefix_runs == 1:
                started.set()
                release.wait(5)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(index_module.subprocess, "run", _fake_run)
    assert schedule_reindex("/product", "/vault", reason="first") is True
    assert started.wait(5)
    assert schedule_reindex("/product", "/vault", reason="second") is False
    assert schedule_reindex("/product", "/vault", reason="third") is False
    release.set()
    _wait_idle()
    assert prefix_runs == 2


def test_failed_run_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        if cmd[1].endswith("contextual-prefix.py"):
            attempts += 1
            if attempts == 1:
                return subprocess.CompletedProcess(cmd, returncode=6, stdout="", stderr="locked")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(index_module.subprocess, "run", _fake_run)
    schedule_reindex("/product", "/vault", reason="test")
    _wait_idle()
    assert attempts == 2


def test_unexpected_exception_frees_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    crashed = threading.Event()
    prefix_runs = 0

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal prefix_runs
        if cmd[1].endswith("contextual-prefix.py"):
            prefix_runs += 1
            if prefix_runs == 1:
                crashed.set()
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(index_module.subprocess, "run", _fake_run)
    assert schedule_reindex("/product", "/vault", reason="first") is True
    assert crashed.wait(5)
    _wait_idle()
    assert schedule_reindex("/product", "/vault", reason="second") is True
    _wait_idle()
    assert prefix_runs == 2


def test_gives_up_after_retries_and_frees_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        raise subprocess.TimeoutExpired(cmd, timeout=1)

    monkeypatch.setattr(index_module.subprocess, "run", _fake_run)
    schedule_reindex("/product", "/vault", reason="test")
    _wait_idle()
    assert attempts == 3  # initial attempt + 2 retries (patched delays)
    assert schedule_reindex("/product", "/vault", reason="again") is True
    _wait_idle()
