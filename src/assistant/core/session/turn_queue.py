"""
Component ID: CMP_CORE_SESSION_TURN_QUEUE

In-process FIFO serialization of the turns of one session.
"""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import structlog

from assistant.core.session.context import SESSION_LOCK_WAIT_TIMEOUT_SECONDS
from assistant.store.interfaces import LockAcquisitionError

logger = structlog.get_logger(__name__)


@dataclass
class _SessionSlot:
    # asyncio.Lock grants waiters strictly in arrival order, never lets a newcomer
    # overtake a waiter, and wakes the next waiter when a waiting task is cancelled.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Turns holding or waiting for the lock; the entry is dropped when it reaches 0.
    users: int = 0


class SessionTurnQueue:
    """Runs the turns of one session one at a time, in arrival order.

    Every producer of turns (user messages, delegation feedback, /compact) enters
    ``slot(session_id)`` before it reads any session state, so a turn starts only
    after the previous turn of the session has been persisted. Different sessions
    never wait for each other. Ordering is per process: the session file lock in
    ``SessionContext`` remains the cross-process guard.

    A turn waits at most ``wait_timeout_seconds`` for its slot, then fails with
    ``LockAcquisitionError`` like a session lock timeout.
    """

    def __init__(self, wait_timeout_seconds: float = SESSION_LOCK_WAIT_TIMEOUT_SECONDS) -> None:
        self._wait_timeout_seconds = wait_timeout_seconds
        self._slots: dict[str, _SessionSlot] = {}

    def depth(self, session_id: str) -> int:
        """Turns of ``session_id`` currently running or waiting (0 when idle)."""
        slot = self._slots.get(session_id)
        return slot.users if slot is not None else 0

    def __len__(self) -> int:
        """Number of sessions with a running or waiting turn."""
        return len(self._slots)

    @asynccontextmanager
    async def slot(self, session_id: str, *, trace_id: str, turn_kind: str) -> AsyncIterator[None]:
        """Hold the turn slot of ``session_id`` for the body of the ``async with``."""
        entry = self._slots.get(session_id)
        if entry is None:
            entry = self._slots[session_id] = _SessionSlot()
        ahead = entry.users
        entry.users += 1
        started = time.monotonic()
        try:
            if ahead:
                logger.info(
                    "orchestrator.turn_queued",
                    session_id=session_id,
                    trace_id=trace_id,
                    turn_kind=turn_kind,
                    queue_depth=ahead,
                )
            try:
                async with asyncio.timeout(self._wait_timeout_seconds):
                    await entry.lock.acquire()
            except TimeoutError:
                wait_ms = _elapsed_ms(started)
                logger.error(
                    "orchestrator.turn_queue_timeout",
                    session_id=session_id,
                    trace_id=trace_id,
                    turn_kind=turn_kind,
                    wait_ms=wait_ms,
                    timeout_seconds=self._wait_timeout_seconds,
                )
                raise LockAcquisitionError(
                    f"Session {session_id} stayed busy with another turn (waited {wait_ms} ms)"
                ) from None
            except asyncio.CancelledError:
                logger.info(
                    "orchestrator.turn_queue_cancelled",
                    session_id=session_id,
                    trace_id=trace_id,
                    turn_kind=turn_kind,
                    wait_ms=_elapsed_ms(started),
                )
                raise
            try:
                if ahead:
                    logger.info(
                        "orchestrator.turn_dequeued",
                        session_id=session_id,
                        trace_id=trace_id,
                        turn_kind=turn_kind,
                        wait_ms=_elapsed_ms(started),
                    )
                yield
            finally:
                entry.lock.release()
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._slots[session_id]


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
