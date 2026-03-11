"""
Component ID: CMP_CHANNEL_TELEGRAM_ADAPTER

Per-user sliding-window throttle guard for inbound Telegram updates.
Prevents accidental message loops by enforcing a per-user rate limit.
"""

import time
from collections import deque

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_WINDOW_SECONDS: float = 60.0
_DEFAULT_MAX_PER_WINDOW: int = 20


class ThrottledError(Exception):
    """Raised when a user's inbound rate exceeds the configured window limit."""

    def __init__(self, user_id: int, count: int, limit: int) -> None:
        super().__init__(f"User {user_id} throttled: {count} events in window (limit={limit})")
        self.user_id = user_id
        self.count = count
        self.limit = limit


class ChannelThrottleGuard:
    """
    Sliding-window rate limiter for inbound channel events.

    Tracks per-user event timestamps within a rolling time window.
    Raises ThrottledError when the count reaches the configured limit.

    Designed for single-loop async use; not thread-safe across threads.
    """

    def __init__(
        self,
        max_per_window: int = _DEFAULT_MAX_PER_WINDOW,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
    ) -> None:
        self._max = max_per_window
        self._window = window_seconds
        self._buckets: dict[int, deque[float]] = {}

    def check(self, user_id: int, trace_id: str = "") -> None:
        """
        Record a new event for user_id.

        Raises ThrottledError when the count within the window reaches the limit.
        Logs a warning with the throttle context on rejection.
        """
        now = time.monotonic()
        timestamps = self._buckets.setdefault(user_id, deque())
        cutoff = now - self._window
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()

        count = len(timestamps)
        if count >= self._max:
            logger.warning(
                "telegram.throttle.rejected",
                user_id=user_id,
                count=count,
                limit=self._max,
                trace_id=trace_id,
            )
            raise ThrottledError(user_id=user_id, count=count, limit=self._max)

        timestamps.append(now)

    def current_count(self, user_id: int) -> int:
        """Return the number of events recorded within the current window for user_id."""
        now = time.monotonic()
        timestamps = self._buckets.get(user_id)
        if not timestamps:
            return 0
        cutoff = now - self._window
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        return len(timestamps)
