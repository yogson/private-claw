"""Tests for obsidian_wiki_search tool."""

import json
import subprocess
from typing import Any

import pytest

from assistant.agent.tools.obsidian_wiki_search import obsidian_wiki_search


class _Ctx:
    pass


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
