"""Tests for obsidian_wiki_search tool."""

import json
import subprocess
from typing import Any

import pytest

from assistant.agent.tools.obsidian_wiki_search import obsidian_wiki_search


class _Ctx:
    pass


@pytest.fixture(autouse=True)
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture background reindex requests; default to a fresh index."""
    reasons: list[str] = []

    def _fake_schedule(product_root: str, vault: str, *, reason: str) -> bool:
        reasons.append(reason)
        return True

    monkeypatch.setattr(
        "assistant.agent.tools.obsidian_wiki_search.schedule_reindex", _fake_schedule
    )
    monkeypatch.setattr(
        "assistant.agent.tools.obsidian_wiki_search.index_is_stale", lambda vault: False
    )
    return reasons


def test_returns_unavailable_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_OBSIDIAN_VAULT", raising=False)
    monkeypatch.delenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", raising=False)
    result = obsidian_wiki_search(_Ctx(), query="anything")
    assert result["status"] == "unavailable"
    assert result["matches"] == []


def test_parses_candidates_into_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", "/vault")
    monkeypatch.setenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", "/product")

    payload = {
        "candidates": [
            {"page_path": "wiki/foo.md", "snippet": "hello", "bm25_score": 1.5},
        ]
    }

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert cmd[0] == "python3"
        assert cmd[1] == "/product/scripts/retrieve.py"
        assert "--vault" in cmd and "/vault" in cmd
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_search.subprocess.run", _fake_run)
    result = obsidian_wiki_search(_Ctx(), query="foo", top=3)
    assert result["status"] == "ok"
    assert result["matches"] == [{"page_path": "wiki/foo.md", "snippet": "hello", "score": 1.5}]


def test_not_provisioned_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", "/vault")
    monkeypatch.setenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", "/product")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=10, stdout="", stderr="no index")

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_search.subprocess.run", _fake_run)
    result = obsidian_wiki_search(_Ctx(), query="foo")
    assert result["status"] == "not_provisioned"
    assert result["matches"] == []


def test_nonzero_exit_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", "/vault")
    monkeypatch.setenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", "/product")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=2, stdout="", stderr="usage error")

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_search.subprocess.run", _fake_run)
    result = obsidian_wiki_search(_Ctx(), query="foo")
    assert result["status"] == "failed"
    assert result["reason"] == "usage error"


def test_timeout_returns_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", "/vault")
    monkeypatch.setenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", "/product")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd, timeout=20)

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_search.subprocess.run", _fake_run)
    result = obsidian_wiki_search(_Ctx(), query="foo")
    assert result["status"] == "failed"


def _ok_run(payload: dict[str, Any]) -> Any:
    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=json.dumps(payload), stderr="")

    return _fake_run


def test_fresh_index_does_not_schedule_reindex(
    monkeypatch: pytest.MonkeyPatch, scheduled: list[str]
) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", "/vault")
    monkeypatch.setenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", "/product")
    monkeypatch.setattr(
        "assistant.agent.tools.obsidian_wiki_search.subprocess.run", _ok_run({"candidates": []})
    )
    result = obsidian_wiki_search(_Ctx(), query="foo")
    assert result["status"] == "ok"
    assert "index_stale" not in result
    assert scheduled == []


def test_stale_index_serves_results_and_schedules_reindex_after_query(
    monkeypatch: pytest.MonkeyPatch, scheduled: list[str]
) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", "/vault")
    monkeypatch.setenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", "/product")
    monkeypatch.setattr(
        "assistant.agent.tools.obsidian_wiki_search.index_is_stale", lambda vault: True
    )
    payload = {"candidates": [{"page_path": "wiki/a.md", "snippet": "s", "bm25_score": 1.0}]}

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # The reindex must not start before retrieve.py has read the index.
        assert scheduled == []
        return _ok_run(payload)(cmd)

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_search.subprocess.run", _fake_run)
    result = obsidian_wiki_search(_Ctx(), query="foo")
    assert result["status"] == "ok"
    assert result["index_stale"] is True
    assert len(result["matches"]) == 1
    assert scheduled == ["obsidian_wiki_search:stale_index"]


def test_not_provisioned_schedules_rebuild(
    monkeypatch: pytest.MonkeyPatch, scheduled: list[str]
) -> None:
    monkeypatch.setenv("CLAUDE_OBSIDIAN_VAULT", "/vault")
    monkeypatch.setenv("CLAUDE_OBSIDIAN_PRODUCT_ROOT", "/product")

    def _fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode=10, stdout="", stderr="no index")

    monkeypatch.setattr("assistant.agent.tools.obsidian_wiki_search.subprocess.run", _fake_run)
    result = obsidian_wiki_search(_Ctx(), query="foo")
    assert result["status"] == "not_provisioned"
    assert scheduled == ["obsidian_wiki_search:not_provisioned"]
