"""
Unit tests for ChannelThrottleGuard.
"""

import time

import pytest

from assistant.channels.telegram.reliability.throttle import (
    ChannelThrottleGuard,
    ThrottledError,
)


class TestChannelThrottleGuard:
    def test_allows_messages_under_limit(self) -> None:
        guard = ChannelThrottleGuard(max_per_window=5)
        for _ in range(5):
            guard.check(user_id=1)

    def test_raises_on_exceeding_limit(self) -> None:
        guard = ChannelThrottleGuard(max_per_window=3)
        for _ in range(3):
            guard.check(user_id=1)
        with pytest.raises(ThrottledError) as exc_info:
            guard.check(user_id=1)
        assert exc_info.value.user_id == 1
        assert exc_info.value.count == 3
        assert exc_info.value.limit == 3

    def test_different_users_are_tracked_independently(self) -> None:
        guard = ChannelThrottleGuard(max_per_window=2)
        guard.check(user_id=1)
        guard.check(user_id=1)
        guard.check(user_id=2)  # user 2 has its own bucket
        with pytest.raises(ThrottledError):
            guard.check(user_id=1)  # user 1 is now throttled
        guard.check(user_id=2)  # user 2 still has budget

    def test_window_expiry_resets_count(self) -> None:
        guard = ChannelThrottleGuard(max_per_window=1, window_seconds=0.05)
        guard.check(user_id=1)
        time.sleep(0.1)
        guard.check(user_id=1)  # window expired; should succeed

    def test_current_count_reflects_window(self) -> None:
        guard = ChannelThrottleGuard(max_per_window=10, window_seconds=0.05)
        guard.check(user_id=1)
        guard.check(user_id=1)
        assert guard.current_count(1) == 2
        time.sleep(0.1)
        assert guard.current_count(1) == 0

    def test_current_count_unknown_user_returns_zero(self) -> None:
        guard = ChannelThrottleGuard()
        assert guard.current_count(999) == 0

    def test_throttled_error_message_includes_context(self) -> None:
        guard = ChannelThrottleGuard(max_per_window=1)
        guard.check(user_id=42)
        with pytest.raises(ThrottledError) as exc_info:
            guard.check(user_id=42)
        assert "42" in str(exc_info.value)
        assert "limit=1" in str(exc_info.value)
