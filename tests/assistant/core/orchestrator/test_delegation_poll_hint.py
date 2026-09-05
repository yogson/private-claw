"""Tests for the anti-polling hint attached to delegation status/log results."""

from assistant.core.orchestrator.service import (
    _DELEGATION_STILL_RUNNING_HINT,
    _with_still_running_hint,
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
