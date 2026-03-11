"""
Unit tests for AllowlistGuard.
"""

import pytest

from assistant.channels.telegram.allowlist import AllowlistGuard, UnauthorizedUserError


def test_allowed_user_passes() -> None:
    guard = AllowlistGuard([111, 222])
    assert guard.is_allowed(111) is True
    assert guard.is_allowed(222) is True


def test_unknown_user_rejected() -> None:
    guard = AllowlistGuard([111])
    assert guard.is_allowed(999) is False


def test_empty_allowlist_rejects_all() -> None:
    guard = AllowlistGuard([])
    assert guard.is_allowed(111) is False


def test_require_allowed_raises_for_unknown() -> None:
    guard = AllowlistGuard([111])
    with pytest.raises(UnauthorizedUserError) as exc_info:
        guard.require_allowed(999)
    assert exc_info.value.user_id == 999


def test_require_allowed_passes_for_known() -> None:
    guard = AllowlistGuard([111])
    guard.require_allowed(111)  # should not raise


def test_unauthorized_error_message() -> None:
    err = UnauthorizedUserError(42)
    assert "42" in str(err)
