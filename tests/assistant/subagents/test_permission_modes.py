"""Tests for the shared Claude Code permission-mode helpers."""

import pytest
from structlog.testing import capture_logs

from assistant.subagents.permission_modes import (
    PERMISSION_MODE_RANK,
    SUPPORTED_PERMISSION_MODES,
    PermissionModeMonitor,
    clamp_permission_mode,
    explain_cli_rejection,
    parse_permission_mode,
    permission_mode_mismatch_error,
    resolve_permission_mode,
)


def test_rank_covers_every_supported_mode_with_a_strict_order() -> None:
    assert set(PERMISSION_MODE_RANK) == set(SUPPORTED_PERMISSION_MODES)
    assert len(set(PERMISSION_MODE_RANK.values())) == len(PERMISSION_MODE_RANK)
    ordered = sorted(PERMISSION_MODE_RANK, key=PERMISSION_MODE_RANK.__getitem__)
    assert ordered == ["plan", "dontAsk", "default", "acceptEdits", "auto", "bypassPermissions"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("auto", "auto"),
        (" acceptEdits ", "acceptEdits"),
        ("manual", "default"),
        ("MANUAL", "default"),
        # Mode names are case-sensitive, as in the CLI.
        ("AUTO", None),
        ("BypassPermissions", None),
        ("bypass", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_parse_permission_mode(raw: object, expected: str | None) -> None:
    assert parse_permission_mode(raw) == expected


@pytest.mark.parametrize("raw", ["BypassPermissions", "AUTO", "yolo"])
def test_case_variants_fall_back_to_default_never_bypass(raw: str) -> None:
    with capture_logs() as logs:
        mode = resolve_permission_mode(raw, "acceptEdits", source="test", task_id="t1")
    assert mode == "acceptEdits"
    assert logs[0]["event"] == "subagent.permission_mode.unknown"
    assert logs[0]["task_id"] == "t1"


def test_clamp_keeps_equal_or_more_restrictive_modes() -> None:
    for ceiling in SUPPORTED_PERMISSION_MODES:
        for requested in SUPPORTED_PERMISSION_MODES:
            with capture_logs() as logs:
                result = clamp_permission_mode(requested, ceiling, task_id="t1")
            if PERMISSION_MODE_RANK[requested] <= PERMISSION_MODE_RANK[ceiling]:
                assert result == requested
                assert logs == []
            else:
                assert result == ceiling
                assert logs[0]["event"] == "subagent.permission_mode.clamped"
                assert logs[0]["task_id"] == "t1"


def test_mismatch_error_wording_is_about_model_response_not_tool_calls() -> None:
    error = permission_mode_mismatch_error("auto", "default", "claude-haiku-4-5")
    assert error is not None
    assert "aborted before the model's first response" in error
    assert "before any tool call" not in error
    assert "claude-haiku-4-5" in error


def test_hint_does_not_recommend_dont_ask() -> None:
    """dontAsk denies AskUserQuestion in the CLI itself, breaking the user relay."""
    error = permission_mode_mismatch_error("auto", "default", "m")
    assert error is not None
    assert "dontAsk" not in error
    assert "'acceptEdits'" in error


def test_cli_rejection_hint_names_no_specific_version() -> None:
    error = explain_cli_rejection("auto", "error: option '--permission-mode <mode>' ...")
    assert error is not None
    assert "supports --permission-mode auto" in error
    assert "2.1.228" not in error


def test_monitor_uses_requested_model_until_a_system_event_names_one() -> None:
    monitor = PermissionModeMonitor("auto", "t1", model="claude-sonnet-4-5")
    error = monitor.observe("system", {"subtype": "status", "permissionMode": "default"})
    assert error is not None
    assert "model 'claude-sonnet-4-5'" in error


def test_monitor_keeps_checking_after_non_model_events() -> None:
    monitor = PermissionModeMonitor("auto", "t1")
    assert monitor.observe("system", {"subtype": "init", "permissionMode": "auto"}) is None
    assert monitor.observe("other", {}) is None  # e.g. rate_limit_event
    error = monitor.observe("system", {"subtype": "status", "permissionMode": "default"})
    assert error is not None


def test_monitor_only_logs_changes_after_model_output() -> None:
    monitor = PermissionModeMonitor("auto", "t1")
    assert monitor.observe("system", {"permissionMode": "auto"}) is None
    assert monitor.observe("model_output", {}) is None
    with capture_logs() as logs:
        assert monitor.observe("system", {"permissionMode": "default"}) is None
    assert logs[0]["event"] == "subagent.permission_mode.changed"
