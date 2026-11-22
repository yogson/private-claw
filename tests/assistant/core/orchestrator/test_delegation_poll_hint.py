"""Tests for delegation status/log results: anti-polling hint and workspace view."""

from datetime import UTC, datetime
from typing import Any

from assistant.core.orchestrator.service import (
    _DELEGATION_STILL_RUNNING_HINT,
    _delegation_status_payload,
    _with_still_running_hint,
)
from assistant.store.models import TaskRecord, TaskStatus


def _task(metadata: dict[str, Any], status: TaskStatus = TaskStatus.RUNNING) -> TaskRecord:
    now = datetime.now(UTC)
    return TaskRecord(
        task_id="dlg-1",
        parent_session_id="tg:1",
        task_type="delegation",
        status=status,
        created_at=now,
        updated_at=now,
        metadata=metadata,
    )


def test_hint_added_for_pending_status() -> None:
    payload = _with_still_running_hint({"found": True, "status": "pending"})
    assert payload["hint"] == _DELEGATION_STILL_RUNNING_HINT


def test_hint_added_for_running_status() -> None:
    payload = _with_still_running_hint({"found": True, "status": "running"})
    assert payload["hint"] == _DELEGATION_STILL_RUNNING_HINT


def test_hint_omitted_for_terminal_statuses() -> None:
    for status in ("completed", "failed", "cancelled", "expired"):
        payload = _with_still_running_hint({"found": True, "status": status})
        assert "hint" not in payload


def test_hint_omitted_when_status_missing() -> None:
    payload = _with_still_running_hint({"found": False, "task_id": "dlg-1"})
    assert "hint" not in payload


def test_status_payload_surfaces_the_resolved_worktree() -> None:
    workspace = {
        "mode": "worktree",
        "directory": "/wt/repo-1/subagent-dlg-1-abc123",
        "requested_directory": "/src/repo",
        "is_git": True,
        "repo_root": "/src/repo",
        "checkout_root": "/src/repo",
        "branch": "subagent/dlg-1",
        "base": "origin/master",
        "base_sha": "f" * 40,
        "worktree": "/wt/repo-1/subagent-dlg-1-abc123",
        "worktree_created": True,
    }
    payload = _delegation_status_payload(_task({"workspace": workspace}), ["line"])
    assert payload["found"] is True
    assert payload["recent_log"] == ["line"]
    assert payload["hint"] == _DELEGATION_STILL_RUNNING_HINT
    assert payload["workspace"] == {
        "mode": "worktree",
        "directory": "/wt/repo-1/subagent-dlg-1-abc123",
        "branch": "subagent/dlg-1",
        "worktree": "/wt/repo-1/subagent-dlg-1-abc123",
        "base": "origin/master",
        "worktree_created": True,
    }


def test_status_payload_workspace_is_null_without_workspace() -> None:
    payload = _delegation_status_payload(_task({}, TaskStatus.COMPLETED), [])
    assert payload["workspace"] is None
    assert "hint" not in payload
